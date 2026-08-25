from __future__ import annotations

import asyncio
import base64
import binascii
import json
import secrets
import time
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import reduce
from operator import or_ as bit_or
from typing import Any, cast

import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import and_, delete, exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.channels import (
    MESSAGE_FLAG_FAILED_TO_MENTION_SOME_ROLES_IN_THREAD,
    admit_thread_message_members,
    capture_thread_message_projection,
    publish_current_thread_member_updates,
    require_message_encryption_policy,
    thread_structural_state_before_message,
)
from app.api.dependencies import AuthenticatedUser, get_redis, get_session, get_snowflake
from app.api.e2ee import (
    RoomActivationRequest,
    RoomProposalRequest,
    RoomRekeyActivationRequest,
    activate_room_encryption_attested,
    activate_room_rekey_attested,
    claim_local_room_key_packages,
    propose_room_encryption,
    propose_room_rekey,
    room_encryption_operation_status_for_actor,
)
from app.auth.instance_restrictions import (
    require_remote_user_creation_allowed,
    require_remote_user_join_allowed,
)
from app.auth.tokens import AccessGrant
from app.bootstrap import MAX_ADVERTISED_OLD_KEYS
from app.bots.installations import (
    cleanup_installation_roles,
    publish_deleted_installation_roles,
    revoke_installations_for_guild_member,
)
from app.chat.custom_emojis import validate_custom_emoji_use
from app.chat.e2ee import (
    channel_encryption_policy_payload,
    validate_channel_encryption_policy,
    validate_channel_encryption_policy_transition,
    validate_e2ee_envelope,
)
from app.chat.e2ee_controls import (
    authority_attested_direct_dm_control,
    authority_attested_room_policy_change,
    room_policy_change_context,
)
from app.chat.e2ee_membership import (
    e2ee_policy_destinations,
    pause_guild_e2ee_for_membership_change,
    pause_local_e2ee_for_device_change,
    publish_e2ee_policy_updates,
)
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.group_conversations import (
    apply_authoritative_group_mutation,
    create_group_mutation_notice,
    group_conversation_content,
    group_participants,
    load_authoritative_group,
    reload_group_projection,
    require_group_invite_friend,
    require_group_member,
)
from app.chat.guild_revision import (
    queue_guild_access_revocation,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.mentions import merge_mention_recipients, role_mention_recipients
from app.chat.moderation_status import guild_self_moderation_status, sanitize_timeout_reason
from app.chat.payloads import (
    channel_payload,
    dm_channel_payload,
    guild_payload,
    message_payload,
    render_message_payload,
    rich_thread_member_payload,
    thread_member_payload,
    user_payload,
)
from app.chat.permissions import (
    PermissionOverwrite,
    require_permissions,
    resolve_permissions,
)
from app.chat.privacy import require_can_direct_message
from app.chat.thread_limits import require_active_thread_capacity
from app.chat.thread_membership import (
    RemovedThreadMembers,
    cleanup_guild_member_threads,
    publish_guild_thread_member_cleanup,
)
from app.core.dm import dm_authority_domain, dm_pair_key
from app.core.federation import (
    FEDERATION_CAPABILITIES,
    canonical_json,
    guild_media_delete_request_ref,
    terminal_room_event_ref,
    terminal_room_generation,
    verify_envelope,
)
from app.core.json_limits import strict_json_loads
from app.core.metrics import increment_metric
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import MAX_SNOWFLAKE, EntityRef, Snowflake
from app.db.models import (
    Attachment,
    Ban,
    Channel,
    ChannelOverwrite,
    DMConversation,
    DMParticipant,
    E2EEControlRecord,
    Emoji,
    FederationEvent,
    FederationInbox,
    FederationOutbox,
    Guild,
    GuildEvent,
    GuildInstanceBan,
    GuildMember,
    Instance,
    Invite,
    MediaTombstoneDestination,
    MediaTombstoneSource,
    MemberRole,
    Message,
    MessageProjection,
    PeerKey,
    Pin,
    Reaction,
    RemoteGuildMembershipIntent,
    RemoteMediaCache,
    RemoteMediaTombstone,
    Role,
    RoomFederationRecipient,
    Sticker,
    TerminalRoomDeletion,
    ThreadMember,
    User,
)
from app.federation.client import signed_request
from app.federation.delivery import FederationOutboxCapacityExceeded
from app.federation.dm_history import MAX_DM_HISTORY_RESPONSE_BYTES
from app.federation.dm_storage import (
    FederatedDMQuotaExceeded,
    admit_federated_dm_conversation,
    dm_authority_history_available,
    dm_history_metadata,
    register_federated_dm_conversation,
)
from app.federation.events import (
    attachment_refs_from_payloads,
    build_envelope,
    locked_retained_media_delete_events,
    media_delete_generation,
    media_delete_order,
    message_attachment_refs,
    metadata_room_ref,
    queue_event,
    record_attachment_recipients,
    record_disclosed_attachment_recipients,
    record_room_federation_recipient,
)
from app.federation.guilds import (
    GUILD_MUTATION_EVENT_TYPES,
    HISTORY_ACCESS_MUTATION_EVENT_TYPES,
    REMOTE_GUILD_JOINING,
    GuildSequenceGap,
    apply_guild_access_revocation,
    apply_guild_instance_access_revocation,
    apply_guild_member_event,
    apply_guild_message_event,
    apply_guild_mutation_event,
    apply_guild_redaction_event,
    assign_guild_sequence,
    guild_event_channel_ref,
    guild_event_for_message,
    guild_event_requires_snapshot,
    guild_history_requires_snapshot,
    guild_snapshot_payload,
    guild_snapshot_rate_scope,
    lock_proxy_nonce,
    mark_guild_replica_stale,
    remote_destinations_with_channel_access,
    store_guild_event,
)
from app.federation.history import (
    complete_history_export,
    create_history_export,
    history_export_delta,
    history_export_manifest,
    history_export_page,
)
from app.federation.identity_storage import FederationIdentityQuotaExceeded
from app.federation.network import (
    FederationInstanceQuotaExceeded,
    FederationNetworkError,
    normalize_domain,
    peer_key_needs_refresh,
)
from app.federation.presence import receive_presence
from app.federation.relationships import (
    RelationshipApplication,
    RelationshipQuotaExceeded,
    apply_relationship_event,
)
from app.federation.replica_storage import (
    REPLICA_QUOTA_ERROR_CODE,
    FederationReplicaQuotaExceeded,
    admit_replica_storage,
    mark_replica_capacity_paused,
    mark_replica_quota_paused,
)
from app.federation.replication import (
    advance_channel_cursor,
    database_snowflake,
    profile_from_user,
    publish_replicated_dm_message,
    replicate_conversation,
    replicate_dm_message,
    replicate_group_notice,
    replicate_message_attachments,
    upsert_remote_user,
)
from app.federation.schemas import (
    DMGroupAuthorizeRequest,
    DMGroupMutationRequest,
    DMOpenFederationRequest,
    E2EEKeyPackageClaimRequest,
    E2EERoomOperationStatusRequest,
    E2EERoomProxyRequest,
    EventEnvelope,
    GuildHistoryExportRequest,
    GuildJoinRequest,
    GuildLeaveRequest,
    GuildPinProxyRequest,
    GuildProxyRequest,
    GuildReactionProxyRequest,
    InboxResult,
    InviteResolveRequest,
    PresenceFederationRequest,
    RemoteUserProfile,
)
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    authenticate_federation_websocket,
    enforce_federation_link_frame_rate_limit,
    enforce_federation_route_rate_limit,
    enforce_origin_event_rate_limit,
    event_timestamp_allowed,
    federation_event_policy_code,
    refresh_event_signing_keys,
    require_guild_federation_access,
    self_instance,
)
from app.federation.storage import (
    current_federation_storage_usage,
    federation_storage_quota_exceeded,
)
from app.federation.terminal_rooms import (
    lock_terminal_room,
    queue_terminal_room_deletion,
    terminal_room_base_content,
)
from app.media.payloads import terminal_attachment_update_payload
from app.media.service import discard_attachment
from app.media.storage import S3Storage, StorageError
from app.media.tombstones import (
    historical_attachment_destinations_by_ref,
    lock_media_tombstone_ref,
    lock_terminal_room_media_fences,
    prepare_terminal_channel_media,
    prepare_terminal_guild_media,
    prepare_terminal_room_media_by_ref,
    queue_terminal_attachment_tombstone,
    record_media_tombstone_destinations,
    terminal_attachment_refs_for_messages,
)
from app.tasks import (
    federation_deliver,
    federation_guild_sync,
    federation_history_sync_guild,
    media_cache_gc,
    media_local_purge,
    media_remote_purge,
    mentions_fanout,
)
from app.voice.rooms import parse_participant_identity, participant_identity
from app.voice.schemas import CallResponse
from app.voice.state import create_call

router = APIRouter(tags=["federation"])
log = structlog.get_logger()

MAX_SNAPSHOT_VISIBILITY_MEMBERS = 100_000
MAX_SNAPSHOT_VISIBILITY_ROLES = 10_000
MAX_SNAPSHOT_VISIBILITY_CHANNELS = 10_000
MAX_SNAPSHOT_VISIBILITY_MEMBER_ROLES = 500_000
MAX_SNAPSHOT_VISIBILITY_OVERWRITES = 100_000
MAX_SNAPSHOT_VISIBILITY_CHECKS = 1_000_000
FEDERATION_LINK_SUBPROTOCOL = "kaede-fed.1"
MAX_LINK_FRAME_BYTES = 1024 * 1024
MAX_INBOUND_LINK_AGE_SECONDS = 55 * 60
LINK_ADMIT_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', tonumber(ARGV[1]) - 90000)
redis.call('ZADD', KEYS[1], tonumber(ARGV[1]), ARGV[2])
redis.call('PEXPIRE', KEYS[1], 180000)
return redis.call('ZCARD', KEYS[1])
"""
LINK_HEARTBEAT_LUA = """
if redis.call('ZSCORE', KEYS[1], ARGV[2]) then
  redis.call('ZADD', KEYS[1], tonumber(ARGV[1]), ARGV[2])
  redis.call('PEXPIRE', KEYS[1], 180000)
  return 1
end
return 0
"""


async def heartbeat_federation_link(
    redis: Redis, key: str, owner: str, websocket: WebSocket
) -> None:
    try:
        while True:
            await asyncio.sleep(30)
            renewed = await cast(Any, redis.eval)(
                LINK_HEARTBEAT_LUA,
                1,
                key,
                str(int(time.time() * 1000)),
                owner,
            )
            if int(renewed) != 1:
                raise RuntimeError("federation link lease was lost")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("federation_link_heartbeat_failed")
        with suppress(Exception):
            await websocket.close(code=1011)


class FederationResyncRetry(RuntimeError):
    """A valid resync marker could not yet complete its callback sync."""


class RemoteMediaTombstoneQuotaExceeded(RuntimeError):
    """A valid media tombstone must be retried after retention capacity frees."""

    federation_code = "KAED_FED_REMOTE_MEDIA_TOMBSTONE_QUOTA_EXCEEDED"


@router.websocket("/_kaede/v1/link")
async def federation_link(websocket: WebSocket) -> None:
    """Carry durable inbox batches over one reusable, signed WebSocket link."""

    offered = {
        item.strip()
        for item in websocket.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if item.strip()
    }
    if FEDERATION_LINK_SUBPROTOCOL not in offered:
        await websocket.close(code=4400)
        return
    sessionmaker = cast(async_sessionmaker[AsyncSession], websocket.app.state.sessionmaker)
    redis = cast(Redis, websocket.app.state.redis)
    snowflake = cast(SnowflakeGenerator, websocket.app.state.snowflake)
    principal: FederationPrincipal | None = None
    connection_key: str | None = None
    connection_owner: str | None = None
    heartbeat: asyncio.Task[None] | None = None
    try:
        async with sessionmaker() as session:
            try:
                principal = await authenticate_federation_websocket(
                    websocket, session, redis, get_settings()
                )
            except HTTPException:
                await websocket.close(code=4401)
                return
            # Authentication's final shared policy fence is transaction scoped.
            # A WebSocket may live for hours, so release it before accepting and
            # reacquire the fence for each individual event below.
            await session.rollback()
            connection_key = f"federation:link:connections:{principal.origin}"
            connection_owner = secrets.token_urlsafe(18)
            active = int(
                await cast(Any, redis.eval)(
                    LINK_ADMIT_LUA,
                    1,
                    connection_key,
                    str(int(time.time() * 1000)),
                    connection_owner,
                )
            )
            if active > 4:
                await redis.zrem(connection_key, connection_owner)
                connection_key = None
                connection_owner = None
                await websocket.close(code=4429)
                return
            heartbeat = asyncio.create_task(
                heartbeat_federation_link(redis, connection_key, connection_owner, websocket)
            )
            await websocket.accept(subprotocol=FEDERATION_LINK_SUBPROTOCOL)
            await websocket.send_json(
                {
                    "op": "hello",
                    "version": "1",
                    "max_batch": 100,
                    "max_frame_bytes": MAX_LINK_FRAME_BYTES,
                    "heartbeat_interval_ms": 30_000,
                }
            )
            link_deadline = time.monotonic() + MAX_INBOUND_LINK_AGE_SECONDS
            local_frame_tokens = 30.0
            local_byte_tokens = float(2 * 1024 * 1024)
            local_budget_updated = time.monotonic()
            while True:
                remaining = link_deadline - time.monotonic()
                if remaining <= 0:
                    await websocket.close(code=1000)
                    return
                try:
                    async with asyncio.timeout(remaining):
                        raw = await websocket.receive_text()
                except TimeoutError:
                    await websocket.close(code=1000)
                    return
                byte_length = len(raw.encode("utf-8"))
                now_monotonic = time.monotonic()
                elapsed = max(0.0, now_monotonic - local_budget_updated)
                local_budget_updated = now_monotonic
                local_frame_tokens = min(30.0, local_frame_tokens + elapsed * 10.0)
                local_byte_tokens = min(
                    float(2 * 1024 * 1024),
                    local_byte_tokens + elapsed * 512 * 1024,
                )
                if local_frame_tokens < 1 or local_byte_tokens < byte_length:
                    await websocket.close(code=4429)
                    return
                local_frame_tokens -= 1
                local_byte_tokens -= byte_length
                try:
                    await enforce_federation_link_frame_rate_limit(
                        redis,
                        principal.origin,
                        byte_length,
                    )
                except HTTPException:
                    await websocket.close(code=4429)
                    return
                if byte_length > MAX_LINK_FRAME_BYTES:
                    await websocket.close(code=4409)
                    return
                try:
                    frame = strict_json_loads(raw)
                except ValueError:
                    await websocket.close(code=4400)
                    return
                if not isinstance(frame, dict):
                    await websocket.close(code=4400)
                    return
                if frame.get("op") == "ping":
                    await websocket.send_json({"op": "pong", "ts": int(time.time() * 1000)})
                    continue
                request_id = frame.get("id")
                raw_events = frame.get("events")
                if (
                    frame.get("op") != "events"
                    or not isinstance(request_id, str)
                    or not 1 <= len(request_id) <= 64
                    or not isinstance(raw_events, list)
                    or not 1 <= len(raw_events) <= 100
                ):
                    await websocket.close(code=4400)
                    return
                await enforce_origin_event_rate_limit(redis, principal.origin, len(raw_events))
                results: list[dict[str, object]] = []
                for raw_event in raw_events:
                    try:
                        event = EventEnvelope.model_validate(raw_event)
                    except ValidationError:
                        event_id = (
                            str(raw_event.get("event_id", ""))[:64]
                            if isinstance(raw_event, dict)
                            else ""
                        )
                        results.append(
                            InboxResult(
                                event_id=event_id,
                                status="rejected",
                                code="KAED_FED_INVALID_EVENT",
                            ).model_dump()
                        )
                        continue
                    policy_code = await federation_event_policy_code(
                        session,
                        principal.origin,
                        event.type,
                        deletion_control=(
                            event.type == "media.delete"
                            or terminal_room_event_ref(event.model_dump(mode="json")) is not None
                            or guild_media_delete_request_ref(event.model_dump(mode="json"))
                            is not None
                        ),
                    )
                    if policy_code is not None:
                        results.append(
                            InboxResult(
                                event_id=event.event_id,
                                status="retry",
                                code=policy_code,
                            ).model_dump()
                        )
                        await session.rollback()
                        continue
                    result = await process_event(
                        session, redis, get_settings(), principal, event, snowflake
                    )
                    results.append(result.model_dump())
                    if session.in_transaction():
                        # Structural/signature rejections can return before the
                        # inbox path commits. Never retain their shared policy
                        # lock while waiting for the next WebSocket frame.
                        await session.rollback()
                await websocket.send_json({"op": "results", "id": request_id, "results": results})
    except WebSocketDisconnect:
        return
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        if connection_key is not None and connection_owner is not None:
            try:
                await redis.zrem(connection_key, connection_owner)
            except Exception:
                log.exception("federation_link_counter_release_failed")


def active_invite(invite: Invite | None) -> bool:
    now = datetime.now(UTC)
    return bool(
        invite is not None
        and invite.revoked_at is None
        and (invite.expires_at is None or invite.expires_at > now)
        and (invite.max_uses is None or invite.uses < invite.max_uses)
    )


async def home_guild(
    session: AsyncSession,
    settings: Settings,
    guild_id: int,
    *,
    for_update: bool = False,
    for_share: bool = False,
) -> Guild:
    if for_update and for_share:
        raise ValueError("guild lock mode is ambiguous")
    statement = select(Guild).where(
        Guild.id == guild_id,
        Guild.origin_domain == settings.domain,
    )
    if for_update:
        statement = statement.with_for_update()
    elif for_share:
        statement = statement.with_for_update(read=True)
    guild = await session.scalar(statement)
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return guild


async def authoritative_dm_conversation(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    profiles: list[RemoteUserProfile],
) -> tuple[DMConversation, Channel, list[User], bool]:
    handles = [f"{profile.username}@{profile.origin_domain}" for profile in profiles]
    authority = dm_authority_domain(*handles)
    if authority != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "KAED_DM_WRONG_AUTHORITY"})
    pair_key = dm_pair_key(*handles)
    participant_domains = {profile.origin_domain for profile in profiles}
    federated = await admit_federated_dm_conversation(
        session,
        settings,
        authority_domain=authority,
        pair_key=pair_key,
        participant_domains=participant_domains,
    )
    users = [await upsert_remote_user(session, settings, profile) for profile in profiles]
    candidate_id = await snowflake.mint()
    inserted_id = await session.scalar(
        pg_insert(DMConversation)
        .values(
            id=candidate_id,
            origin_domain=settings.domain,
            pair_key=pair_key,
            type="direct",
            authority_domain=settings.domain,
        )
        .on_conflict_do_nothing(index_elements=["pair_key"])
        .returning(DMConversation.id)
    )
    created = inserted_id is not None
    if created:
        conversation = await session.get(DMConversation, (candidate_id, settings.domain))
        if conversation is None:
            raise RuntimeError("new DM authority conversation disappeared")
        if federated:
            await register_federated_dm_conversation(
                session,
                settings,
                conversation,
                participant_domains=participant_domains,
            )
        channel = Channel(
            id=candidate_id,
            origin_domain=settings.domain,
            guild_id=None,
            guild_domain=None,
            type=1,
            name=None,
            position=0,
            rate_limit_per_user=0,
            created_floor_id=candidate_id,
        )
        session.add(channel)
        await session.flush()
        session.add_all(
            [
                DMParticipant(
                    conversation_id=candidate_id,
                    conversation_domain=settings.domain,
                    user_id=user.id,
                    user_domain=user.origin_domain,
                )
                for user in users
            ]
        )
        await session.flush()
    else:
        conversation = await session.scalar(
            select(DMConversation).where(DMConversation.pair_key == pair_key)
        )
        if conversation is None:
            raise RuntimeError("concurrent DM authority open did not converge")
        loaded_channel = await session.get(Channel, (conversation.id, conversation.origin_domain))
        if (
            loaded_channel is None
            or loaded_channel.guild_id is not None
            or loaded_channel.type != 1
        ):
            raise RuntimeError("DM authority conversation has no valid channel")
        channel = loaded_channel
        participant_refs = set(
            (
                await session.execute(
                    select(DMParticipant.user_id, DMParticipant.user_domain).where(
                        DMParticipant.conversation_id == conversation.id,
                        DMParticipant.conversation_domain == conversation.origin_domain,
                    )
                )
            ).tuples()
        )
        if participant_refs != {(user.id, user.origin_domain) for user in users}:
            raise RuntimeError("DM authority pair key has inconsistent participants")
        if federated:
            await register_federated_dm_conversation(
                session,
                settings,
                conversation,
                participant_domains=participant_domains,
            )
    return conversation, channel, users, created


async def validated_guild_mentions(
    session: AsyncSession,
    guild: Guild,
    refs: list[tuple[int, str]],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for user_id, raw_domain in dict.fromkeys(refs):
        domain = normalize_domain(raw_domain)
        member = await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, user_id, domain),
        )
        if member is None:
            raise HTTPException(status_code=400, detail={"code": "KAED_GUILD_INVALID_MENTION"})
        result.append({"id": str(user_id), "origin_domain": domain})
    return result


async def has_outbound_dm_open_request(
    session: AsyncSession,
    destination: str,
    pair_key: str,
    local_user: User,
) -> bool:
    request_id = await session.scalar(
        select(FederationEvent.event_id)
        .join(
            FederationOutbox,
            (FederationOutbox.event_origin_domain == FederationEvent.origin_domain)
            & (FederationOutbox.event_id == FederationEvent.event_id),
        )
        .where(
            FederationOutbox.destination == destination,
            FederationEvent.origin_domain == local_user.origin_domain,
            FederationEvent.event_type == "dm.open.request",
            FederationEvent.envelope["content"]["pair_key"].as_string() == pair_key,
            FederationEvent.envelope["actor"]["id"].as_string() == str(local_user.id),
            FederationEvent.envelope["actor"]["domain"].as_string() == local_user.origin_domain,
        )
        .limit(1)
    )
    return request_id is not None


async def has_outbound_guild_proxy(
    session: AsyncSession,
    destination: str,
    guild_id: int,
    channel_id: int,
    client_nonce: str,
    local_user: User,
) -> bool:
    request_id = await session.scalar(
        select(FederationEvent.event_id)
        .join(
            FederationOutbox,
            (FederationOutbox.event_origin_domain == FederationEvent.origin_domain)
            & (FederationOutbox.event_id == FederationEvent.event_id),
        )
        .where(
            FederationOutbox.destination == destination,
            FederationEvent.origin_domain == local_user.origin_domain,
            FederationEvent.event_type == "guild.proxy.message.create",
            FederationEvent.envelope["context"]["guild_id"].as_string() == str(guild_id),
            FederationEvent.envelope["content"]["channel_id"].as_string() == str(channel_id),
            FederationEvent.envelope["content"]["client_nonce"].as_string() == client_nonce,
            FederationEvent.envelope["actor"]["id"].as_string() == str(local_user.id),
            FederationEvent.envelope["actor"]["domain"].as_string() == local_user.origin_domain,
        )
        .limit(1)
    )
    return request_id is not None


@router.get("/.well-known/kaede/server")
async def well_known(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    capabilities = list(FEDERATION_CAPABILITIES)
    if not settings.search_enabled:
        capabilities.remove("message-search/1")
    return {
        "server": settings.domain,
        "versions": ["1", "2"],
        "capabilities": capabilities,
    }


@router.get("/_kaede/v1/keys")
async def federation_keys(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    instance = await self_instance(session, settings)
    keys = list(
        await session.scalars(
            select(PeerKey).where(
                PeerKey.domain == settings.domain,
                PeerKey.expired_at.is_(None),
            )
        )
    )
    current: dict[str, str] = {}
    old: dict[str, str] = {}
    for key in keys:
        encoded = base64.b64encode(key.public_key).decode("ascii")
        if key.key_id == instance.current_key_id:
            current[key.key_id] = encoded
        else:
            old[key.key_id] = encoded
    if len(old) > MAX_ADVERTISED_OLD_KEYS:
        # Refuse an ambiguous partial trust document. Routine rotation prevents
        # this state; reaching it indicates legacy/manual key data that an
        # operator must retire deliberately.
        raise HTTPException(
            status_code=503,
            detail={"code": "KAED_FED_KEY_HISTORY_OVERFLOW"},
        )
    return {
        "server_name": settings.domain,
        "display_name": instance.display_name,
        "software_version": "0.1.0",
        "current_key_id": instance.current_key_id,
        "verify_keys": current,
        "old_verify_keys": old,
    }


def verify_event_signature(
    envelope: EventEnvelope, peer_key: PeerKey, encoded_signature: str
) -> bool:
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
    except (ValueError, binascii.Error):
        return False
    if len(signature) != 64 or peer_key.expired_at is not None:
        return False
    return verify_envelope(
        envelope.model_dump(mode="json"),
        signature,
        Ed25519PublicKey.from_public_bytes(peer_key.public_key),
    )


def validated_rejection_timeout_reason(value: object) -> str | None:
    """Validate and display-sanitize a user-scoped federation rejection reason."""

    if value is not None and (not isinstance(value, str) or len(value) > 512):
        raise ValueError("write rejection timeout reason is invalid")
    return sanitize_timeout_reason(value)


async def _apply_authoritative_guild_leave(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    *,
    user_id: int,
    user_domain: str,
    missing_ok: bool,
    e2ee_policy_channels: list[Channel] | None = None,
) -> tuple[bool, list[tuple[int, str]], list[RemovedThreadMembers]]:
    """Apply an idempotent remote leave at the guild authority."""

    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, user_id, user_domain),
    )
    if member is None:
        if missing_ok:
            return False, [], []
        raise HTTPException(status_code=404, detail={"code": "NOT_A_GUILD_MEMBER"})
    if (guild.owner_id, guild.owner_domain) == (user_id, user_domain):
        raise HTTPException(
            status_code=409,
            detail={"code": "OWNER_MUST_TRANSFER_OR_DELETE_GUILD"},
        )
    owner = await session.get(User, (guild.owner_id, guild.owner_domain))
    if owner is None or not owner.is_local:
        raise RuntimeError("local guild owner is unavailable")
    revoked_installations = await revoke_installations_for_guild_member(
        session,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=user_id,
        user_domain=user_domain,
    )
    deleted_role_refs = await cleanup_installation_roles(
        session,
        settings,
        guild,
        owner,
        revoked_installations,
    )
    removed_thread_members = await cleanup_guild_member_threads(
        session,
        settings,
        guild,
        owner,
        [(user_id, user_domain)],
    )
    await session.delete(member)
    await queue_guild_access_revocation(
        session,
        settings,
        guild,
        user_id=user_id,
        user_domain=user_domain,
        reason="member_left",
    )
    leaving_user = await session.get(User, (user_id, user_domain))
    await queue_guild_mutation(
        session,
        settings,
        guild,
        owner,
        "guild.member.remove",
        {"user": {"id": str(user_id), "origin_domain": user_domain}},
        snapshot_required=True,
        e2ee_policy_channels=e2ee_policy_channels,
        pause_e2ee=leaving_user is None or leaving_user.account_type != "bot",
    )
    return True, deleted_role_refs, removed_thread_members


async def remote_guild_snapshot_is_pending(
    session: AsyncSession,
    settings: Settings,
    guild_id: int,
    guild_domain: str,
) -> bool:
    """Return whether a local user is actively installing this remote guild."""

    return (
        await session.scalar(
            select(RemoteGuildMembershipIntent.user_id)
            .where(
                RemoteGuildMembershipIntent.guild_id == guild_id,
                RemoteGuildMembershipIntent.guild_domain == guild_domain,
                RemoteGuildMembershipIntent.user_domain == settings.domain,
                RemoteGuildMembershipIntent.state == REMOTE_GUILD_JOINING,
            )
            .limit(1)
        )
        is not None
    )


async def media_delete_cascade_is_complete(
    session: AsyncSession,
    settings: Settings,
    *,
    attachment_id: int,
    attachment_domain: str,
    event_id: str,
    upstream_domain: str,
) -> bool:
    """Return whether every downstream recipient acknowledged this proof.

    A relay must keep its upstream sender retrying until all children have
    durably accepted the selected origin-signed generation.  Otherwise the
    origin can compact its source proof and retire its signing key while a
    transitive recipient is still offline.
    """

    destinations = await historical_attachment_destinations_by_ref(
        session,
        attachment_id,
        attachment_domain,
    )
    destinations.difference_update(
        {settings.domain, attachment_domain, normalize_domain(upstream_domain)}
    )
    if not destinations:
        return True
    delivered = set(
        await session.scalars(
            select(FederationOutbox.destination).where(
                FederationOutbox.destination.in_(destinations),
                FederationOutbox.event_origin_domain == attachment_domain,
                FederationOutbox.event_id == event_id,
                FederationOutbox.status == "delivered",
            )
        )
    )
    return destinations <= delivered


def post_commit_inbox_result(event_id: str, *, media_delete_cascade_pending: bool) -> InboxResult:
    """Preserve transitive media acknowledgement after a durable commit."""

    if media_delete_cascade_pending:
        return InboxResult(
            event_id=event_id,
            status="retry",
            code="KAED_FED_MEDIA_DELETE_CASCADE_PENDING",
        )
    return InboxResult(event_id=event_id, status="accepted")


def superseded_media_delete_result(
    event_id: str,
    *,
    incoming_generation: int,
    selected_event_id: str,
    selected_generation: int,
) -> InboxResult | None:
    """Classify a proof that cannot advance the retained generation."""

    if incoming_generation < selected_generation:
        return InboxResult(event_id=event_id, status="duplicate")
    if incoming_generation == selected_generation and event_id != selected_event_id:
        return InboxResult(
            event_id=event_id,
            status="rejected",
            code="KAED_FED_MEDIA_DELETE_GENERATION_CONFLICT",
        )
    return None


async def process_event(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: FederationPrincipal,
    envelope: EventEnvelope,
    snowflake: SnowflakeGenerator,
) -> InboxResult:
    authority_attested_group_types = {
        "dm.group.state",
        "dm.group.message.committed",
        "dm.group.call.create",
    }
    relayed_media_delete = envelope.type == "media.delete" and envelope.origin != principal.origin
    authority_attested_direct_control = authority_attested_direct_dm_control(
        envelope.type,
        envelope.content,
        expected_authority=envelope.origin,
        actor_id=envelope.actor.id,
        actor_domain=envelope.actor.domain,
    )
    authority_attested_policy_change = authority_attested_room_policy_change(
        envelope.type,
        envelope.content,
        envelope.context,
        expected_authority=envelope.origin,
        actor_id=envelope.actor.id,
        actor_domain=envelope.actor.domain,
    )
    if (envelope.origin != principal.origin and not relayed_media_delete) or (
        envelope.actor.domain != principal.origin
        and envelope.type not in authority_attested_group_types
        and not authority_attested_direct_control
        and not authority_attested_policy_change
        and not relayed_media_delete
    ):
        return InboxResult(
            event_id=envelope.event_id,
            status="rejected",
            code="KAED_FED_AUTHOR_ORIGIN_MISMATCH",
        )
    if envelope.type == "media.delete" and envelope.actor.domain != envelope.origin:
        # Relays transport the origin's exact signed proof; they do not gain
        # authority to attribute that proof to a user on another instance.
        return InboxResult(
            event_id=envelope.event_id,
            status="rejected",
            code="KAED_FED_AUTHOR_ORIGIN_MISMATCH",
        )
    if relayed_media_delete:
        origin_policy_code = await federation_event_policy_code(
            session,
            envelope.origin,
            envelope.type,
            deletion_control=True,
        )
        if origin_policy_code is not None:
            return InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code=origin_policy_code,
            )
    serialized_envelope = envelope.model_dump(mode="json")
    terminal_room_ref = terminal_room_event_ref(serialized_envelope)
    guild_media_request_ref = guild_media_delete_request_ref(serialized_envelope)
    if not event_timestamp_allowed(
        envelope.ts,
        now_ms=int(time.time() * 1000),
        future_skew_seconds=settings.federation_clock_skew_seconds,
        retention_days=settings.federation_event_retention_days,
        # Origin-signed media tombstones are permanent invalidation records.
        # A peer recovering after a long outage must still accept them; future
        # skew remains bounded and signature/replay checks still apply.
        allow_past=(
            envelope.type == "media.delete"
            or terminal_room_ref is not None
            or guild_media_request_ref is not None
        ),
    ):
        return InboxResult(
            event_id=envelope.event_id,
            status="rejected",
            code="KAED_FED_EVENT_TIMESTAMP_INVALID",
        )
    signatures = envelope.signatures.get(envelope.origin, {})

    async def verify_cached_event_signatures() -> tuple[bool, str | None]:
        refresh_key: str | None = None
        for key_id, encoded in signatures.items():
            peer_key = await session.get(PeerKey, (envelope.origin, key_id))
            if peer_key is None or peer_key_needs_refresh(peer_key, datetime.now(UTC)):
                refresh_key = refresh_key or key_id
                continue
            if verify_event_signature(envelope, peer_key, encoded):
                return True, refresh_key
        return False, refresh_key

    valid, refresh_key = await verify_cached_event_signatures()
    if not valid and refresh_key is not None:
        signing_principal = (
            FederationPrincipal(
                origin=envelope.origin,
                key_id=refresh_key,
                silenced=False,
                source_ip=principal.source_ip,
            )
            if relayed_media_delete
            else principal
        )
        refreshed = await refresh_event_signing_keys(
            session,
            redis,
            settings,
            signing_principal,
            refresh_key,
        )
        if not refreshed:
            await session.rollback()
            return InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code="KAED_FED_UNKNOWN_KEY",
            )
        valid, _unused_refresh_key = await verify_cached_event_signatures()
    if not valid:
        return InboxResult(
            event_id=envelope.event_id, status="rejected", code="KAED_FED_BAD_EVENT_SIGNATURE"
        )
    ordinary_room_ref = metadata_room_ref(serialized_envelope)
    ordinary_attachment_refs = message_attachment_refs(serialized_envelope)
    if guild_media_request_ref is not None:
        (
            request_guild_id,
            request_guild_domain,
            _request_message_id,
            _request_message_domain,
            request_attachment_id,
            request_attachment_domain,
            _request_generation,
        ) = guild_media_request_ref
        await lock_terminal_room(
            session,
            "guild",
            request_guild_id,
            request_guild_domain,
        )
        await lock_media_tombstone_ref(
            session,
            request_attachment_id,
            request_attachment_domain,
        )
    if ordinary_room_ref is not None and terminal_room_ref is None:
        # A durable terminal receipt is a no-resurrection fence for every
        # delayed pre-delete room event. Serialize this check with disclosure
        # and terminal apply before claiming inbox quota or creating replicas.
        await lock_terminal_room(session, *ordinary_room_ref)
        if (
            await session.get(
                TerminalRoomDeletion,
                (*ordinary_room_ref, settings.domain),
                populate_existing=True,
            )
            is not None
        ):
            await session.rollback()
            return InboxResult(event_id=envelope.event_id, status="duplicate")
    if terminal_room_ref is None and guild_media_request_ref is None:
        # Every attachment-bearing writer takes its media fences before the
        # global quota Instance row.  queue_event/replication later reacquire
        # these transaction-scoped advisory locks, preserving the canonical
        # room -> media -> global -> outbox order.
        for attachment_id, attachment_domain in sorted(
            ordinary_attachment_refs, key=lambda ref: (ref[1], ref[0])
        ):
            await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    envelope_bytes = len(canonical_json(serialized_envelope))
    media_attachment_ref: tuple[int, str] | None = None
    media_replacement_event: FederationEvent | None = None
    media_replacement_inbox: FederationInbox | None = None
    media_signing_key_id: str | None = None
    terminal_room_receipt: TerminalRoomDeletion | None = None
    terminal_room_replacement_event: FederationEvent | None = None
    terminal_room_replacement_inbox: FederationInbox | None = None
    terminal_room_signing_key_id: str | None = None
    terminal_room_incoming_generation: int | None = None
    past_ordinary_retention = envelope.ts < int(
        (datetime.now(UTC) - timedelta(days=settings.federation_event_retention_days)).timestamp()
        * 1000
    )
    if guild_media_request_ref is not None:
        request_attachment_ref = (
            guild_media_request_ref[4],
            guild_media_request_ref[5],
        )
        if request_attachment_ref[1] != settings.domain:
            return InboxResult(
                event_id=envelope.event_id,
                status="rejected",
                code="KAED_FED_EVENT_INVALID",
            )
        if await session.get(MediaTombstoneSource, request_attachment_ref) is not None:
            await session.rollback()
            return InboxResult(event_id=envelope.event_id, status="duplicate")
        if past_ordinary_retention:
            request_route_exists = (
                await session.scalar(
                    select(MediaTombstoneDestination.attachment_id)
                    .where(
                        MediaTombstoneDestination.attachment_id == request_attachment_ref[0],
                        MediaTombstoneDestination.attachment_domain == request_attachment_ref[1],
                        MediaTombstoneDestination.destination_domain == envelope.origin,
                        MediaTombstoneDestination.room_kind == "guild",
                        MediaTombstoneDestination.room_id == guild_media_request_ref[0],
                        MediaTombstoneDestination.room_domain == guild_media_request_ref[1],
                    )
                    .limit(1)
                )
                is not None
            )
            if (
                await session.get(Attachment, request_attachment_ref) is None
                and not request_route_exists
            ):
                await session.rollback()
                return InboxResult(event_id=envelope.event_id, status="duplicate")
    elif envelope.type == "media.delete":
        try:
            attachment_origin = normalize_domain(str(envelope.content.get("origin_domain", "")))
            if attachment_origin != envelope.origin:
                raise ValueError("media tombstone is not authoritative for the attachment")
            attachment_number = database_snowflake(
                envelope.content.get("attachment_id"), "attachment id"
            )
            incoming_generation = media_delete_generation(serialized_envelope)
            if len(signatures) != 1:
                raise ValueError("media tombstone must have exactly one origin signature")
            media_signing_key_id = next(iter(signatures))
        except (FederationNetworkError, ValueError):
            return InboxResult(
                event_id=envelope.event_id,
                status="rejected",
                code="KAED_FED_EVENT_INVALID",
            )
        media_attachment_ref = (attachment_number, attachment_origin)
        # Serialize generation comparison before quota admission and before the
        # inbox/event claim. A delayed E1 can therefore never overwrite E2,
        # even if both transactions passed signature verification together.
        await locked_retained_media_delete_events(
            session,
            attachment_number,
            attachment_origin,
        )
        await lock_media_tombstone_ref(session, attachment_number, attachment_origin)
        existing_source = await session.scalar(
            select(MediaTombstoneSource)
            .where(
                MediaTombstoneSource.attachment_id == attachment_number,
                MediaTombstoneSource.attachment_domain == attachment_origin,
            )
            .with_for_update()
        )
        if existing_source is not None:
            superseded = superseded_media_delete_result(
                envelope.event_id,
                incoming_generation=incoming_generation,
                selected_event_id=existing_source.event_id,
                selected_generation=existing_source.generation,
            )
            if superseded is not None:
                # Delayed older outboxes may drain; equal-generation
                # equivocation is terminally rejected for operator visibility.
                await session.rollback()
                return superseded
            if incoming_generation == existing_source.generation:
                cascade_complete = bool(
                    await media_delete_cascade_is_complete(
                        session,
                        settings,
                        attachment_id=attachment_number,
                        attachment_domain=attachment_origin,
                        event_id=existing_source.event_id,
                        upstream_domain=principal.origin,
                    )
                )
                await session.rollback()
                return InboxResult(
                    event_id=envelope.event_id,
                    status="duplicate" if cascade_complete else "retry",
                    code=(None if cascade_complete else "KAED_FED_MEDIA_DELETE_CASCADE_PENDING"),
                )
            media_replacement_event = await session.get(
                FederationEvent,
                (attachment_origin, existing_source.event_id),
            )
            media_replacement_inbox = await session.get(
                FederationInbox,
                (attachment_origin, existing_source.event_id),
            )
        elif past_ordinary_retention:
            has_live_media_state = any(
                (
                    await session.get(
                        Attachment,
                        (attachment_number, attachment_origin),
                    )
                    is not None,
                    await session.get(
                        RemoteMediaTombstone,
                        (attachment_origin, attachment_number),
                    )
                    is not None,
                    await session.scalar(
                        select(RemoteMediaCache.attachment_id)
                        .where(
                            RemoteMediaCache.origin_domain == attachment_origin,
                            RemoteMediaCache.attachment_id == attachment_number,
                        )
                        .limit(1)
                    )
                    is not None,
                    await session.scalar(
                        select(MediaTombstoneDestination.attachment_id)
                        .where(
                            MediaTombstoneDestination.attachment_id == attachment_number,
                            MediaTombstoneDestination.attachment_domain == attachment_origin,
                        )
                        .limit(1)
                    )
                    is not None,
                )
            )
            if not has_live_media_state:
                await session.rollback()
                return InboxResult(event_id=envelope.event_id, status="duplicate")
    elif terminal_room_ref is not None:
        room_kind, room_id, room_domain = terminal_room_ref
        try:
            terminal_room_incoming_generation = terminal_room_generation(serialized_envelope)
            if len(signatures) != 1:
                raise ValueError("terminal room deletion must have one origin signature")
            terminal_room_signing_key_id = next(iter(signatures))
        except ValueError:
            return InboxResult(
                event_id=envelope.event_id,
                status="rejected",
                code="KAED_FED_EVENT_INVALID",
            )
        # Close the room's bound + routed media set before quota admission.
        # Terminal apply can safely reuse all three transaction locks below.
        await lock_terminal_room_media_fences(
            session,
            room_kind=room_kind,
            room_id=room_id,
            room_domain=room_domain,
        )
        terminal_room_receipt = await session.scalar(
            select(TerminalRoomDeletion)
            .where(
                TerminalRoomDeletion.room_kind == room_kind,
                TerminalRoomDeletion.room_id == room_id,
                TerminalRoomDeletion.room_domain == room_domain,
                TerminalRoomDeletion.destination_domain == settings.domain,
            )
            .with_for_update()
        )
        if terminal_room_receipt is not None:
            incoming_terminal_content = terminal_room_base_content(serialized_envelope)
            if (
                terminal_room_receipt.actor_id
                != database_snowflake(envelope.actor.id, "terminal room actor id")
                or terminal_room_receipt.actor_domain != envelope.actor.domain
                or terminal_room_receipt.event_type != envelope.type
                or terminal_room_receipt.content != incoming_terminal_content
                or terminal_room_receipt.context != envelope.context
            ):
                await session.rollback()
                return InboxResult(
                    event_id=envelope.event_id,
                    status="rejected",
                    code="KAED_FED_AUTHOR_ORIGIN_MISMATCH",
                )
            if terminal_room_incoming_generation <= terminal_room_receipt.generation:
                await session.rollback()
                return InboxResult(event_id=envelope.event_id, status="duplicate")
            terminal_room_replacement_event = await session.get(
                FederationEvent,
                (room_domain, terminal_room_receipt.event_id),
            )
            terminal_room_replacement_inbox = await session.get(
                FederationInbox,
                (room_domain, terminal_room_receipt.event_id),
            )
        elif past_ordinary_retention:
            room_route_exists = (
                await session.scalar(
                    select(MediaTombstoneDestination.attachment_id)
                    .where(
                        MediaTombstoneDestination.room_kind == room_kind,
                        MediaTombstoneDestination.room_id == room_id,
                        MediaTombstoneDestination.room_domain == room_domain,
                    )
                    .limit(1)
                )
                is not None
            )
            room_recipient_exists = (
                await session.scalar(
                    select(RoomFederationRecipient.room_id)
                    .where(
                        RoomFederationRecipient.room_kind == room_kind,
                        RoomFederationRecipient.room_id == room_id,
                        RoomFederationRecipient.room_domain == room_domain,
                    )
                    .limit(1)
                )
                is not None
            )
            if room_kind == "guild":
                room_projection_exists = (
                    await session.get(Guild, (room_id, room_domain)) is not None
                )
            else:
                room_projection_exists = (
                    await session.get(DMConversation, (room_id, room_domain)) is not None
                    or await session.get(Channel, (room_id, room_domain)) is not None
                )
            if not (room_route_exists or room_recipient_exists or room_projection_exists):
                await session.rollback()
                return InboxResult(event_id=envelope.event_id, status="duplicate")
    prior = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
    if prior is not None:
        if prior.status == "processed":
            result = InboxResult(event_id=envelope.event_id, status="duplicate")
        elif prior.status == "rejected":
            result = InboxResult(
                event_id=envelope.event_id,
                status="rejected",
                code=prior.result_code or "KAED_FED_EVENT_REJECTED",
            )
        else:
            result = InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code="KAED_FED_EVENT_RETRY",
            )
        await session.rollback()
        return result
    # The self Instance row is the singleton global quota ledger. Lock it
    # before the origin row everywhere so concurrent origins cannot each admit
    # against a stale global total and retention cannot deadlock admission.
    global_ledger = await session.scalar(
        select(Instance).where(Instance.is_self.is_(True)).with_for_update()
    )
    if global_ledger is None:
        await session.rollback()
        return InboxResult(
            event_id=envelope.event_id,
            status="retry",
            code="KAED_FED_EVENT_RETRY",
        )
    peer = await session.scalar(
        select(Instance)
        .where(Instance.domain == envelope.origin, Instance.is_self.is_(False))
        .with_for_update()
    )
    if peer is None:
        await session.rollback()
        return InboxResult(
            event_id=envelope.event_id,
            status="retry",
            code="KAED_FED_EVENT_RETRY",
        )
    usage = current_federation_storage_usage(peer, global_ledger)
    if federation_storage_quota_exceeded(
        settings,
        usage,
        incoming_bytes=envelope_bytes,
        replacing_event=(
            media_replacement_inbox is not None or terminal_room_replacement_inbox is not None
        ),
        replacing_bytes=(
            media_replacement_event.envelope_bytes
            if media_replacement_event is not None
            else (
                terminal_room_replacement_event.envelope_bytes
                if terminal_room_replacement_event is not None
                else 0
            )
        ),
    ):
        await increment_metric(redis, "federation_inbox_quota_rejections")
        await session.rollback()
        return InboxResult(
            event_id=envelope.event_id,
            status="retry",
            code="KAED_FED_INBOX_QUOTA_EXCEEDED",
        )
    claimed = await session.scalar(
        pg_insert(FederationInbox)
        .values(origin_domain=envelope.origin, event_id=envelope.event_id)
        .on_conflict_do_nothing(index_elements=["origin_domain", "event_id"])
        .returning(FederationInbox.event_id)
    )
    if claimed is None:
        prior = await session.get(
            FederationInbox,
            (envelope.origin, envelope.event_id),
            populate_existing=True,
        )
        if prior is None:
            # The conflicting transaction can only disappear by rolling back;
            # do not report success for work whose terminal state is unknown.
            await session.rollback()
            return InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code="KAED_FED_EVENT_RETRY",
            )
        if prior.status == "processed":
            result = InboxResult(event_id=envelope.event_id, status="duplicate")
        elif prior.status == "rejected":
            result = InboxResult(
                event_id=envelope.event_id,
                status="rejected",
                code=prior.result_code or "KAED_FED_EVENT_REJECTED",
            )
        else:
            result = InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code="KAED_FED_EVENT_RETRY",
            )
        await session.rollback()
        return result
    peer.federation_inbox_events += 1
    global_ledger.federation_inbox_events += 1
    # Keep the idempotency claim in the outer transaction. Event application
    # runs in a savepoint so a rejection cannot briefly remove the claim and
    # allow a concurrent replay to apply the same event.
    await session.flush()
    event_work = await session.begin_nested()
    inserted_event = await session.scalar(
        pg_insert(FederationEvent)
        .values(
            event_id=envelope.event_id,
            origin_domain=envelope.origin,
            event_type=envelope.type,
            envelope=serialized_envelope,
            envelope_bytes=envelope_bytes,
            # A verified origin-signed media tombstone is the relay source for
            # replicas that learn about the attachment after the initial
            # verdict. Retain it independently of ordinary history retention.
            expires_at=(
                None
                if envelope.type == "media.delete" or terminal_room_ref is not None
                else datetime.now(UTC) + timedelta(days=settings.federation_event_retention_days)
            ),
        )
        .on_conflict_do_nothing(index_elements=["origin_domain", "event_id"])
        .returning(FederationEvent.event_id)
    )
    if inserted_event is None:
        conflicting_event = await session.get(FederationEvent, (envelope.origin, envelope.event_id))
        if conflicting_event is None or (
            conflicting_event.origin_domain != envelope.origin
            or conflicting_event.envelope != serialized_envelope
        ):
            await event_work.rollback()
            inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
            if inbox is None:
                raise RuntimeError("federation inbox claim disappeared")
            inbox.status = "rejected"
            inbox.result_code = "KAED_FED_EVENT_ID_CONFLICT"
            inbox.error = "event ID conflicts with a different envelope"
            inbox.processed_at = datetime.now(UTC)
            await session.commit()
            return InboxResult(
                event_id=envelope.event_id,
                status="rejected",
                code="KAED_FED_EVENT_ID_CONFLICT",
            )
    inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
    if inbox is None:
        raise RuntimeError("federation inbox claim disappeared")
    replicated_message = None
    replicated_guild_message = None
    replicated_thread_message: Channel | None = None
    replicated_guild = None
    replicated_guild_member: User | None = None
    replicated_guild_dispatch: tuple[str, dict[str, object]] | None = None
    home_message = None
    home_message_attachments: list[Attachment] = []
    home_message_created = False
    home_thread: Channel | None = None
    home_thread_members_added: list[ThreadMember] = []
    home_thread_updated = False
    home_thread_was_unarchived = False
    delivery_wakes: set[str] = set()
    rejection_target: tuple[int, str] | None = None
    rejection_payload: dict[str, object] | None = None
    created_dm_channel: Channel | None = None
    dm_channel_recipient: User | None = None
    group_state_conversation: DMConversation | None = None
    group_state_before: list[User] = []
    group_state_before_refs: set[tuple[int, str]] = set()
    group_state_after: list[User] = []
    group_state_ref: tuple[int, str] | None = None
    group_state_changed = False
    group_notice_ref: tuple[int, str] | None = None
    dm_open_rejection_target: tuple[int, str] | None = None
    dm_open_rejection_payload: dict[str, object] | None = None
    access_revocation_target: tuple[int, str] | None = None
    instance_access_revoked_users: list[int] = []
    media_purge_target: tuple[str, int] | None = None
    media_tombstone_dispatch_payload: dict[str, object] | None = None
    media_tombstone_channel_ref: tuple[int, str] | None = None
    media_tombstone_guild_ref: tuple[int, str] | None = None
    media_delete_cascade_pending = False
    local_media_purge_refs: list[tuple[int, str]] = []
    remote_media_purge_refs: list[tuple[str, int]] = []
    remote_cache_gc_needed = terminal_room_ref is not None
    relationship_application: RelationshipApplication | None = None
    history_access_changed = False
    authoritative_leave_guild: Guild | None = None
    authoritative_leave_target: tuple[int, str] | None = None
    authoritative_leave_role_refs: list[tuple[int, str]] = []
    authoritative_leave_thread_removals: list[RemovedThreadMembers] = []
    replicated_group_call: dict[str, Any] | None = None
    e2ee_policy_channels: list[Channel] = []
    durably_committed = False
    try:
        if guild_media_request_ref is not None:
            (
                request_guild_id,
                request_guild_domain,
                request_message_id,
                request_message_domain,
                request_attachment_id,
                request_attachment_domain,
                _request_generation,
            ) = guild_media_request_ref
            if request_attachment_domain != settings.domain:
                raise ValueError("guild media deletion request was sent to the wrong origin")
            retained_route = await session.get(
                MediaTombstoneDestination,
                (
                    request_attachment_id,
                    request_attachment_domain,
                    request_guild_domain,
                ),
            )
            if retained_route is None or (
                retained_route.room_kind,
                retained_route.room_id,
                retained_route.room_domain,
            ) != ("guild", request_guild_id, request_guild_domain):
                raise ValueError("guild media deletion request has no authoritative route")
            # The exact request is signed by the guild authority and bound to
            # a retained guild/media route above.  Do not compare its actor to
            # a replica that may have gone stale before an ownership transfer;
            # losing visibility is precisely why this durable control exists.
            request_attachment = await session.scalar(
                select(Attachment)
                .where(
                    Attachment.id == request_attachment_id,
                    Attachment.origin_domain == request_attachment_domain,
                )
                .with_for_update()
            )
            if request_attachment is None:
                raise ValueError("guild media deletion request references unknown local media")
            if request_attachment.message_id is not None and (
                request_attachment.message_id,
                request_attachment.message_domain,
            ) != (request_message_id, request_message_domain):
                raise ValueError("guild media deletion request message binding is invalid")
            request_message = await session.get(
                Message,
                (request_message_id, request_message_domain),
            )
            if request_message is not None:
                request_channel = await session.get(
                    Channel,
                    (request_message.channel_id, request_message.channel_domain),
                )
                if request_channel is None or (
                    request_channel.guild_id,
                    request_channel.guild_domain,
                ) != (request_guild_id, request_guild_domain):
                    raise ValueError("guild media deletion request references the wrong room")
            delivery_wakes.update(
                await queue_terminal_attachment_tombstone(
                    session,
                    settings,
                    request_attachment,
                    force_authoritative=True,
                )
            )
            if request_attachment.staging_object_key is None:
                request_attachment.staging_object_key = request_attachment.object_key
            await discard_attachment(session, settings, request_attachment)
            request_attachment.message_id = None
            request_attachment.message_domain = None
            local_media_purge_refs.append((request_attachment.id, request_attachment.origin_domain))
        elif envelope.type in {
            "relationship.request",
            "relationship.accept",
            "relationship.remove",
            "relationship.profile",
        }:
            relationship_application = await apply_relationship_event(
                session,
                settings,
                envelope,
            )
            if relationship_application.wake_destination is not None:
                delivery_wakes.add(relationship_application.wake_destination)
        elif envelope.type == "e2ee.device-list.changed":
            raw_profile = envelope.content.get("profile")
            profile = RemoteUserProfile.model_validate(raw_profile)
            if envelope.origin != envelope.actor.domain or (profile.id, profile.origin_domain) != (
                envelope.actor.id,
                envelope.actor.domain,
            ):
                raise ValueError("E2EE device update is not authoritative for its actor")
            existing_user = await session.get(
                User,
                (database_snowflake(profile.id, "E2EE device user id"), profile.origin_domain),
            )
            if existing_user is None or existing_user.is_local:
                raise ValueError("E2EE device update references an unknown remote participant")
            previous_generation = existing_user.e2ee_device_generation
            changed_user = await upsert_remote_user(session, settings, profile)
            if changed_user.e2ee_device_generation > previous_generation:
                paused = await pause_local_e2ee_for_device_change(session, settings, changed_user)
                e2ee_policy_channels.extend(paused)
                for channel in paused:
                    destinations = await e2ee_policy_destinations(session, settings, channel)
                    if not destinations:
                        continue
                    policy_envelope = await build_envelope(
                        session,
                        settings,
                        "e2ee.room-policy.changed",
                        changed_user,
                        {
                            "channel_id": str(channel.id),
                            "channel_domain": channel.origin_domain,
                            "encryption_policy": channel_encryption_policy_payload(channel),
                        },
                        context=room_policy_change_context(channel, changed_user),
                        authority_attested_actor=True,
                    )
                    for destination in destinations:
                        await queue_event(session, settings, destination, policy_envelope)
                    delivery_wakes.update(destinations)
        elif envelope.type == "e2ee.room-policy.changed":
            if not authority_attested_policy_change:
                raise ValueError("E2EE room policy context is not authority-bound")
            raw_channel_id = envelope.content.get("channel_id")
            raw_channel_domain = envelope.content.get("channel_domain")
            channel_id = database_snowflake(raw_channel_id, "E2EE policy channel id")
            if raw_channel_domain != envelope.origin:
                raise ValueError("E2EE room policy did not originate at its authority")
            loaded_e2ee_channel = await session.get(Channel, (channel_id, envelope.origin))
            if loaded_e2ee_channel is None or loaded_e2ee_channel.unavailable:
                raise ValueError("E2EE room policy references an unknown channel")
            channel = loaded_e2ee_channel
            raw_scope = cast(dict[str, object], envelope.context["scope"])
            scope_id = database_snowflake(raw_scope.get("id"), "E2EE policy scope id")
            scope_domain = normalize_domain(str(raw_scope.get("domain", "")))
            actor_id = database_snowflake(envelope.actor.id, "E2EE policy actor id")
            actor_membership: GuildMember | DMParticipant | None
            if raw_scope.get("type") == "guild":
                if (
                    channel.guild_id is None
                    or channel.guild_domain is None
                    or (channel.guild_id, channel.guild_domain) != (scope_id, scope_domain)
                    or scope_domain != envelope.origin
                ):
                    raise ValueError("E2EE room policy guild context does not match its channel")
                actor_membership = await session.get(
                    GuildMember,
                    (scope_id, scope_domain, actor_id, envelope.actor.domain),
                )
            else:
                if (
                    channel.guild_id is not None
                    or channel.type != 1
                    or (channel.id, channel.origin_domain) != (scope_id, scope_domain)
                ):
                    raise ValueError("E2EE room policy DM context does not match its channel")
                conversation = await session.get(DMConversation, (scope_id, scope_domain))
                if (
                    conversation is None
                    or conversation.origin_domain != envelope.origin
                    or conversation.authority_domain != envelope.origin
                ):
                    raise ValueError("E2EE room policy DM context is not authoritative")
                actor_membership = await session.get(
                    DMParticipant,
                    (scope_id, scope_domain, actor_id, envelope.actor.domain),
                )
            if actor_membership is None:
                raise ValueError("E2EE room policy actor is not a room participant")
            incoming_policy = validate_channel_encryption_policy(
                envelope.content.get("encryption_policy")
            )
            validate_channel_encryption_policy_transition(
                channel, incoming_policy, label="E2EE room"
            )
            channel.encryption_mode = str(incoming_policy["mode"])
            channel.encryption_state = str(incoming_policy["state"])
            channel.encryption_policy_generation = int(incoming_policy["generation"])
            channel.encryption_protocol = incoming_policy["protocol"]
            channel.encryption_suite = incoming_policy["suite"]
            channel.encryption_group_id = incoming_policy["group_id"]
            channel.encryption_epoch = incoming_policy["epoch"]
            e2ee_policy_channels.append(channel)
        elif (
            envelope.type == "dm.group.state"
            and terminal_room_ref is not None
            and await session.get(
                DMConversation,
                (terminal_room_ref[1], terminal_room_ref[2]),
            )
            is None
        ):
            # A current-key regeneration can arrive after the first terminal
            # generation already purged the replica. Independent media routes
            # can outlive that projection, so terminalize them again before
            # acknowledging the proof rather than assuming an empty room.
            (
                missing_group_local_purges,
                missing_group_remote_purges,
                _missing_group_destinations,
                missing_group_wakes,
            ) = await prepare_terminal_room_media_by_ref(
                session,
                settings,
                room_kind="group_dm",
                room_id=terminal_room_ref[1],
                room_domain=terminal_room_ref[2],
            )
            local_media_purge_refs.extend(missing_group_local_purges)
            remote_media_purge_refs.extend(missing_group_remote_purges)
            delivery_wakes.update(missing_group_wakes)
        elif envelope.type == "dm.group.state":
            raw_conversation = envelope.content.get("conversation")
            raw_participants = envelope.content.get("participants")
            if not isinstance(raw_conversation, dict) or not isinstance(raw_participants, list):
                raise ValueError("group DM state is malformed")
            if (
                str(raw_conversation.get("type")) != "group"
                or str(raw_conversation.get("origin_domain")) != envelope.origin
                or str(raw_conversation.get("authority_domain")) != envelope.origin
            ):
                raise ValueError("group DM state is not owned by its authority")
            conversation_id = database_snowflake(
                raw_conversation.get("id"), "group DM conversation id"
            )
            existing_group = await session.get(DMConversation, (conversation_id, envelope.origin))
            previous_group_owner = (
                (existing_group.owner_id, existing_group.owner_domain)
                if existing_group is not None
                else (None, None)
            )
            if existing_group is not None:
                group_state_before = await group_participants(session, existing_group)
            group_state_before_refs = {(user.id, user.origin_domain) for user in group_state_before}
            prior_refs = {(str(user.id), user.origin_domain) for user in group_state_before}
            profiles = [RemoteUserProfile.model_validate(item) for item in raw_participants]
            profile_refs = {(item.id, item.origin_domain) for item in profiles}
            actor_ref = (envelope.actor.id, envelope.actor.domain)
            incoming_state_version = database_snowflake(
                raw_conversation.get("state_version"), "group DM state version"
            )
            if incoming_state_version < 1:
                raise ValueError("group DM state version is invalid")
            older_group_state = (
                existing_group is not None and existing_group.state_version > incoming_state_version
            )
            equal_group_state = (
                existing_group is not None
                and existing_group.state_version == incoming_state_version
            )
            deleted_group_state = raw_conversation.get("deleted") is True
            if older_group_state:
                created_dm_channel = await session.get(Channel, (conversation_id, envelope.origin))
                if created_dm_channel is None:
                    raise ValueError("group DM channel is missing")
                group_state_conversation = existing_group
                group_state_after = group_state_before
            elif equal_group_state and deleted_group_state:
                created_dm_channel = await session.get(Channel, (conversation_id, envelope.origin))
                if created_dm_channel is None or not created_dm_channel.unavailable or profile_refs:
                    raise ValueError("group DM deletion conflicts with stored state")
                group_state_conversation = existing_group
                group_state_after = []
            elif deleted_group_state:
                if existing_group is None:
                    raise ValueError("unknown group DM cannot be deleted")
                # An exact terminal shape is authority-signed and cannot rely
                # on a participant replica that may be stale after access was
                # lost. Ordinary group state still requires a known actor.
                if terminal_room_ref is None and actor_ref not in prior_refs:
                    raise ValueError("group DM state actor is not a participant")
                created_dm_channel = await session.get(Channel, (conversation_id, envelope.origin))
                if created_dm_channel is None:
                    raise ValueError("group DM channel is missing")
                (
                    deleted_group_media_purges,
                    _deleted_group_state_destinations,
                    deleted_group_media_wakes,
                ) = await prepare_terminal_channel_media(
                    session,
                    settings,
                    created_dm_channel,
                )
                local_media_purge_refs.extend(deleted_group_media_purges)
                delivery_wakes.update(deleted_group_media_wakes)
                for membership in list(
                    await session.scalars(
                        select(DMParticipant).where(
                            DMParticipant.conversation_id == conversation_id,
                            DMParticipant.conversation_domain == envelope.origin,
                        )
                    )
                ):
                    await session.delete(membership)
                created_dm_channel.unavailable = True
                existing_group.state_version = incoming_state_version
                group_state_conversation = existing_group
                group_state_after = []
                group_state_changed = True
            else:
                if actor_ref not in (prior_refs or profile_refs):
                    raise ValueError("group DM state actor is not a participant")
                if existing_group is not None:
                    stored_group_channel = await session.get(
                        Channel, (conversation_id, envelope.origin)
                    )
                    if stored_group_channel is None:
                        raise ValueError("group DM channel is missing")
                    if stored_group_channel.unavailable:
                        raise ValueError("a deleted group DM cannot be restored")
                if not equal_group_state:
                    added_local_profiles = [
                        profile
                        for profile in profiles
                        if (profile.id, profile.origin_domain) not in prior_refs
                        and profile.origin_domain == settings.domain
                    ]
                    if added_local_profiles:
                        actor = await session.get(
                            User,
                            (
                                database_snowflake(envelope.actor.id, "group DM actor id"),
                                envelope.actor.domain,
                            ),
                        )
                        if actor is None:
                            actor_profile = next(
                                (
                                    profile
                                    for profile in profiles
                                    if (profile.id, profile.origin_domain) == actor_ref
                                ),
                                None,
                            )
                            if actor_profile is None:
                                raise ValueError("group DM actor profile is missing")
                            actor = await upsert_remote_user(session, settings, actor_profile)
                        for invitee_profile in added_local_profiles:
                            invitee = await upsert_remote_user(session, settings, invitee_profile)
                            await require_group_invite_friend(session, actor, invitee)
                created_dm_channel = await replicate_conversation(
                    session, settings, raw_conversation, profiles
                )
                group_state_conversation = await session.get(
                    DMConversation,
                    (created_dm_channel.id, created_dm_channel.origin_domain),
                )
                if group_state_conversation is None:
                    raise RuntimeError("replicated group DM disappeared")
                group_state_after = await group_participants(session, group_state_conversation)
                group_state_changed = not equal_group_state
            group_state_ref = (conversation_id, envelope.origin)
            raw_notice = envelope.content.get("notice")
            if raw_notice is not None:
                if group_state_conversation is None or created_dm_channel is None:
                    raise ValueError("group DM notice has no conversation state")
                notice_message = await replicate_group_notice(
                    session,
                    settings,
                    raw_notice,
                    group_state_conversation,
                    created_dm_channel,
                    group_state_before,
                    group_state_after,
                    previous_owner=previous_group_owner,
                    expected_actor=(
                        database_snowflake(envelope.actor.id, "group DM notice actor id"),
                        envelope.actor.domain,
                    ),
                    initial_snapshot=existing_group is None,
                    event_timestamp_ms=envelope.ts,
                )
                if notice_message is not None:
                    group_notice_ref = (
                        notice_message.id,
                        notice_message.origin_domain,
                    )
        elif envelope.type == "dm.conversation.create":
            if not any(
                str(item.get("id")) == envelope.actor.id
                and item.get("origin_domain") == envelope.actor.domain
                for item in envelope.content["participants"]
            ):
                raise ValueError("DM actor is not a participant")
            open_request = DMOpenFederationRequest.model_validate(
                {"participants": envelope.content["participants"]}
            )
            profiles = open_request.participants
            handles = [f"{profile.username}@{profile.origin_domain}" for profile in profiles]
            authority = dm_authority_domain(*handles)
            pair_key = dm_pair_key(*handles)
            raw_conversation = envelope.content["conversation"]
            if not isinstance(raw_conversation, dict) or (
                str(raw_conversation.get("origin_domain")) != envelope.origin
                or str(raw_conversation.get("authority_domain")) != authority
                or str(raw_conversation.get("pair_key")) != pair_key
                or authority != envelope.origin
            ):
                raise ValueError("DM conversation is not owned by its deterministic authority")
            local_profiles = [
                profile for profile in profiles if profile.origin_domain == settings.domain
            ]
            if len(local_profiles) != 1:
                raise ValueError("federated DM conversation must contain one local participant")
            local_user = await upsert_remote_user(session, settings, local_profiles[0])
            remote_profile = next(
                profile for profile in profiles if profile.origin_domain != settings.domain
            )
            remote_user = await upsert_remote_user(session, settings, remote_profile)
            if not await has_outbound_dm_open_request(
                session, envelope.origin, pair_key, local_user
            ):
                await require_can_direct_message(session, remote_user, local_user)
            created_dm_channel = await replicate_conversation(
                session, settings, raw_conversation, profiles
            )
            dm_channel_recipient = local_user
        elif envelope.type == "dm.open.request":
            open_request = DMOpenFederationRequest.model_validate(
                {"participants": envelope.content["participants"]}
            )
            profiles = open_request.participants
            if not any(
                profile.id == envelope.actor.id and profile.origin_domain == envelope.actor.domain
                for profile in profiles
            ):
                raise ValueError("DM open actor is not a participant")
            users = [await upsert_remote_user(session, settings, profile) for profile in profiles]
            handles = [f"{user.username}@{user.origin_domain}" for user in users]
            if dm_authority_domain(*handles) != settings.domain or str(
                envelope.content.get("pair_key")
            ) != dm_pair_key(*handles):
                raise ValueError("DM open request was sent to the wrong authority")
            local_recipient = next(
                (user for user in users if user.origin_domain == settings.domain), None
            )
            remote_sender = next(
                (user for user in users if user.origin_domain == envelope.origin), None
            )
            if local_recipient is None or remote_sender is None:
                raise ValueError("DM open request has invalid participants")
            try:
                await require_can_direct_message(session, remote_sender, local_recipient)
            except HTTPException as exc:
                dm_detail: dict[str, object] = exc.detail if isinstance(exc.detail, dict) else {}
                rejected = await build_envelope(
                    session,
                    settings,
                    "dm.open.rejected",
                    local_recipient,
                    {
                        "target": {"id": envelope.actor.id, "domain": envelope.actor.domain},
                        "pair_key": dm_pair_key(*handles),
                        "code": str(dm_detail.get("code") or "KAED_DM_OPEN_REJECTED"),
                    },
                )
                await queue_event(session, settings, envelope.origin, rejected)
            else:
                (
                    conversation,
                    authority_channel,
                    users,
                    conversation_created,
                ) = await authoritative_dm_conversation(session, settings, snowflake, profiles)
                if conversation_created:
                    created_dm_channel = authority_channel
                    dm_channel_recipient = local_recipient
                created = await build_envelope(
                    session,
                    settings,
                    "dm.conversation.create",
                    local_recipient,
                    {
                        "conversation": {
                            "id": str(conversation.id),
                            "origin_domain": conversation.origin_domain,
                            "pair_key": conversation.pair_key,
                            "authority_domain": conversation.authority_domain,
                        },
                        "participants": [profile_from_user(user) for user in users],
                    },
                )
                await queue_event(session, settings, envelope.origin, created)
            delivery_wakes.add(envelope.origin)
        elif envelope.type == "dm.group.call.create":
            call = CallResponse.model_validate(envelope.content.get("call"))
            if call.authority_domain != envelope.origin:
                raise ValueError("group call did not originate at its authority")
            if call.channel_id != str(
                envelope.context.get("conversation_id")
            ) or call.channel_domain != str(envelope.context.get("conversation_domain")):
                raise ValueError("group call context does not match its conversation")
            call_conversation = await session.get(
                DMConversation,
                (int(call.channel_id), call.channel_domain),
            )
            if call_conversation is None:
                raise FederationResyncRetry
            if (
                call_conversation.type != "group"
                or call_conversation.authority_domain != envelope.origin
                or call_conversation.origin_domain != envelope.origin
            ):
                raise ValueError("group call references a non-authoritative conversation")
            required_state_version = database_snowflake(
                envelope.context.get("state_version"),
                "group call state version",
            )
            if call_conversation.state_version < required_state_version:
                raise FederationResyncRetry
            caller_ref = parse_participant_identity(call.caller)
            if caller_ref != (
                database_snowflake(envelope.actor.id, "group call actor id"),
                envelope.actor.domain,
            ):
                raise ValueError("group call actor does not match its caller")
            current_participants = await group_participants(session, call_conversation)
            expected_identities = {
                participant_identity(user.id, user.origin_domain) for user in current_participants
            }
            if set(call.participants) != expected_identities:
                raise ValueError("group call participant set does not match group state")
            replicated_group_call = call.model_dump(mode="json")
        elif envelope.type in {
            "dm.message.create",
            "dm.group.message.proposed",
            "dm.group.message.committed",
        }:
            author = envelope.content["author"]
            if (
                str(author.get("id")) != envelope.actor.id
                or author.get("origin_domain") != envelope.actor.domain
            ):
                raise ValueError("DM event actor does not match message author")
            raw_message = envelope.content.get("message")
            if not isinstance(raw_message, dict):
                raise ValueError("DM message content is malformed")
            conversation_ref = (
                database_snowflake(raw_message.get("channel_id"), "DM channel id"),
                normalize_domain(str(raw_message.get("channel_domain", ""))),
            )
            message_conversation = await session.get(DMConversation, conversation_ref)
            if authority_attested_direct_control and (
                message_conversation is None
                or message_conversation.type != "direct"
                or message_conversation.authority_domain != envelope.origin
                or message_conversation.origin_domain != envelope.origin
            ):
                raise ValueError(
                    "authority-attested DM control is not bound to its direct conversation"
                )
            if envelope.type != "dm.message.create":
                if message_conversation is None:
                    if envelope.type == "dm.group.message.committed":
                        raise FederationResyncRetry
                    raise ValueError("group DM conversation is not replicated")
                if message_conversation.type != "group":
                    raise ValueError("group DM message references a direct conversation")
                if (
                    str(envelope.context.get("conversation_id")) != str(message_conversation.id)
                    or normalize_domain(str(envelope.context.get("conversation_domain", "")))
                    != message_conversation.origin_domain
                ):
                    raise ValueError("group DM message context does not match its conversation")
                required_state_version = database_snowflake(
                    envelope.context.get("state_version"),
                    "group DM message state version",
                )
                if required_state_version > message_conversation.state_version:
                    raise FederationResyncRetry
                if envelope.type == "dm.group.message.proposed" and (
                    message_conversation.authority_domain != settings.domain
                    or message_conversation.origin_domain != settings.domain
                ):
                    raise ValueError("group DM proposal was not sent to its authority")
                if envelope.type == "dm.group.message.committed" and (
                    message_conversation.authority_domain != envelope.origin
                    or message_conversation.origin_domain != envelope.origin
                ):
                    raise ValueError("group DM commit did not originate at its authority")
            replicated_message = await replicate_dm_message(
                session,
                settings,
                envelope.content,
                event_timestamp_ms=envelope.ts,
                event_origin=envelope.origin,
            )
            if replicated_message is None:
                replicated_message = await session.get(
                    Message,
                    (
                        database_snowflake(raw_message.get("id"), "DM message id"),
                        normalize_domain(str(raw_message.get("origin_domain", ""))),
                    ),
                )
            if replicated_message is None:
                raise RuntimeError("replicated DM message disappeared")
            terminal_attachment_refs = await terminal_attachment_refs_for_messages(
                session,
                settings,
                {(replicated_message.id, replicated_message.origin_domain)},
            )
            for terminal_attachment_ref in terminal_attachment_refs:
                terminal_attachment = await session.get(Attachment, terminal_attachment_ref)
                if terminal_attachment is not None:
                    delivery_wakes.update(
                        await queue_terminal_attachment_tombstone(
                            session,
                            settings,
                            terminal_attachment,
                        )
                    )
            if envelope.type == "dm.group.message.proposed":
                if message_conversation is None:
                    raise RuntimeError("validated group DM proposal lost its conversation")
                actor = await session.get(
                    User,
                    (
                        database_snowflake(envelope.actor.id, "group DM message actor id"),
                        envelope.actor.domain,
                    ),
                )
                if actor is None:
                    raise RuntimeError("replicated group DM message author disappeared")
                committed = await build_envelope(
                    session,
                    settings,
                    "dm.group.message.committed",
                    actor,
                    envelope.content,
                    context={
                        "conversation_id": str(message_conversation.id),
                        "conversation_domain": message_conversation.origin_domain,
                        "state_version": str(message_conversation.state_version),
                    },
                    authority_attested_actor=True,
                )
                participants = await group_participants(session, message_conversation)
                committed_destinations = {
                    participant.origin_domain
                    for participant in participants
                    if participant.origin_domain != settings.domain
                }
                for destination in committed_destinations:
                    await queue_event(session, settings, destination, committed)
                delivery_wakes.update(committed_destinations)
        elif envelope.type == "dm.open.rejected":
            target = envelope.content["target"]
            dm_open_rejection_target = (
                database_snowflake(target.get("id"), "DM rejection target id"),
                str(target.get("domain")),
            )
            if dm_open_rejection_target[1] != settings.domain:
                raise ValueError("DM open rejection target is not local")
            target_user = await session.get(User, dm_open_rejection_target)
            pair_key = str(envelope.content["pair_key"])
            if target_user is None or not await has_outbound_dm_open_request(
                session, envelope.origin, pair_key, target_user
            ):
                raise ValueError("DM open rejection has no matching local request")
            dm_open_rejection_payload = {
                "pair_key": pair_key,
                "code": str(envelope.content["code"]),
            }
        elif envelope.type in {"guild.message.create", "guild.message.committed"}:
            raw_message = envelope.content["message"]
            if envelope.type == "guild.message.create" and (
                str(raw_message.get("author_id")) != envelope.actor.id
                or raw_message.get("author_domain") != envelope.actor.domain
            ):
                raise ValueError("guild event actor does not match message author")
            guild_id = database_snowflake(envelope.context.get("guild_id"), "guild id")
            guild_domain = str(envelope.context["guild_domain"])
            if guild_domain != envelope.origin:
                raise ValueError("guild event did not originate at the guild home")
            replicated_guild = await session.get(Guild, (guild_id, guild_domain))
            if replicated_guild is None:
                if await remote_guild_snapshot_is_pending(
                    session, settings, guild_id, guild_domain
                ):
                    raise FederationResyncRetry
                raise ValueError("guild snapshot is required before live events")
            if envelope.type == "guild.message.committed" and (
                database_snowflake(envelope.actor.id, "guild commit actor id"),
                envelope.actor.domain,
            ) != (replicated_guild.owner_id, replicated_guild.owner_domain):
                raise ValueError("guild commit was not signed for the guild owner")
            try:
                replicated_guild_message = await apply_guild_message_event(
                    session,
                    settings,
                    replicated_guild,
                    envelope.model_dump(mode="json"),
                )
            except GuildSequenceGap as exc:
                raise FederationResyncRetry from exc
            if replicated_guild_message is None:
                replicated_guild_message = await session.get(
                    Message,
                    (
                        database_snowflake(raw_message.get("id"), "guild message id"),
                        normalize_domain(str(raw_message.get("origin_domain", ""))),
                    ),
                )
            if replicated_guild_message is None:
                raise RuntimeError("replicated guild message disappeared")
            replicated_message_channel = await session.get(
                Channel,
                (
                    replicated_guild_message.channel_id,
                    replicated_guild_message.channel_domain,
                ),
            )
            if (
                replicated_message_channel is not None
                and replicated_message_channel.type in {10, 11, 12}
                and envelope.content.get("thread_starter") is not True
            ):
                replicated_thread_message = replicated_message_channel
            terminal_attachment_refs = await terminal_attachment_refs_for_messages(
                session,
                settings,
                {
                    (
                        replicated_guild_message.id,
                        replicated_guild_message.origin_domain,
                    )
                },
            )
            for attachment_ref in terminal_attachment_refs:
                terminal_attachment = await session.get(Attachment, attachment_ref)
                if terminal_attachment is not None:
                    delivery_wakes.update(
                        await queue_terminal_attachment_tombstone(
                            session,
                            settings,
                            terminal_attachment,
                        )
                    )
        elif envelope.type == "guild.member.add":
            guild_id = database_snowflake(envelope.context.get("guild_id"), "guild id")
            guild_domain = str(envelope.context.get("guild_domain"))
            if guild_domain != envelope.origin:
                raise ValueError("guild member event did not originate at the guild home")
            replicated_guild = await session.get(Guild, (guild_id, guild_domain))
            if replicated_guild is None:
                if await remote_guild_snapshot_is_pending(
                    session, settings, guild_id, guild_domain
                ):
                    raise FederationResyncRetry
                raise ValueError("guild snapshot is required before live events")
            if (
                database_snowflake(envelope.actor.id, "guild member event actor id"),
                envelope.actor.domain,
            ) != (replicated_guild.owner_id, replicated_guild.owner_domain):
                raise ValueError("guild member event was not signed for the guild owner")
            try:
                applied_member = await apply_guild_member_event(
                    session,
                    settings,
                    replicated_guild,
                    envelope.model_dump(mode="json"),
                )
            except GuildSequenceGap as exc:
                raise FederationResyncRetry from exc
            if applied_member is not None and applied_member[1]:
                replicated_guild_member = applied_member[0]
                if replicated_guild_member.account_type != "bot":
                    paused = await pause_guild_e2ee_for_membership_change(session, replicated_guild)
                    known = {(item.id, item.origin_domain) for item in e2ee_policy_channels}
                    e2ee_policy_channels.extend(
                        item for item in paused if (item.id, item.origin_domain) not in known
                    )
        elif envelope.type == "guild.leave.request":
            guild_id = database_snowflake(envelope.context.get("guild_id"), "guild id")
            guild_domain = normalize_domain(str(envelope.context.get("guild_domain", "")))
            if guild_domain != settings.domain:
                raise ValueError("guild leave request was sent to the wrong authority")
            raw_user = envelope.content.get("user")
            if not isinstance(raw_user, dict) or (
                str(raw_user.get("id")) != envelope.actor.id
                or normalize_domain(str(raw_user.get("domain", ""))) != envelope.actor.domain
            ):
                raise ValueError("guild leave actor does not match its target")
            home_leave_guild = await session.scalar(
                select(Guild)
                .where(Guild.id == guild_id, Guild.origin_domain == settings.domain)
                .with_for_update()
            )
            if home_leave_guild is None:
                raise ValueError("guild leave request references an unknown guild")
            leave_user_id = database_snowflake(envelope.actor.id, "guild leave user id")
            (
                leave_applied,
                deleted_role_refs,
                thread_removals,
            ) = await _apply_authoritative_guild_leave(
                session,
                settings,
                home_leave_guild,
                user_id=leave_user_id,
                user_domain=envelope.actor.domain,
                missing_ok=True,
                e2ee_policy_channels=e2ee_policy_channels,
            )
            if leave_applied:
                authoritative_leave_guild = home_leave_guild
                authoritative_leave_target = (leave_user_id, envelope.actor.domain)
                authoritative_leave_role_refs = deleted_role_refs
                authoritative_leave_thread_removals = thread_removals
        elif envelope.type in GUILD_MUTATION_EVENT_TYPES | {"guild.event.redacted"}:
            guild_id = database_snowflake(envelope.context.get("guild_id"), "guild id")
            guild_domain = normalize_domain(str(envelope.context.get("guild_domain", "")))
            if guild_domain != envelope.origin:
                raise ValueError("guild mutation did not originate at the guild home")
            replicated_guild = await session.get(Guild, (guild_id, guild_domain))
            if replicated_guild is None:
                if await remote_guild_snapshot_is_pending(
                    session, settings, guild_id, guild_domain
                ):
                    raise FederationResyncRetry
                raise ValueError("guild snapshot is required before live mutations")
            mutation_sequence = database_snowflake(
                envelope.context.get("seq"), "guild mutation sequence"
            )
            if (
                guild_event_requires_snapshot(envelope.model_dump(mode="json"))
                and replicated_guild.last_event_seq < mutation_sequence
            ):
                replicated_guild.sync_status = "stale"
                raise FederationResyncRetry
            if envelope.type == "guild.event.redacted":
                if (
                    database_snowflake(envelope.actor.id, "guild redaction actor id"),
                    envelope.actor.domain,
                ) != (replicated_guild.owner_id, replicated_guild.owner_domain):
                    raise ValueError("guild redaction was not signed for the guild owner")
                try:
                    await apply_guild_redaction_event(
                        session,
                        replicated_guild,
                        envelope.model_dump(mode="json"),
                    )
                except GuildSequenceGap as exc:
                    raise FederationResyncRetry from exc
            else:
                try:
                    replicated_guild_dispatch = await apply_guild_mutation_event(
                        session,
                        settings,
                        replicated_guild,
                        envelope.model_dump(mode="json"),
                        e2ee_policy_channels=e2ee_policy_channels,
                    )
                    history_access_changed = envelope.type in HISTORY_ACCESS_MUTATION_EVENT_TYPES
                except GuildSequenceGap as exc:
                    raise FederationResyncRetry from exc
        elif envelope.type == "guild.access.revoked":
            guild_id = database_snowflake(envelope.context.get("guild_id"), "guild id")
            guild_domain = str(envelope.context.get("guild_domain"))
            if guild_domain != envelope.origin:
                raise ValueError("guild access revocation did not originate at the guild home")
            replicated_guild = await session.get(Guild, (guild_id, guild_domain))
            if replicated_guild is None:
                raise ValueError("guild access revocation references an unknown replica")
            if (
                database_snowflake(envelope.actor.id, "guild access revocation actor id"),
                envelope.actor.domain,
            ) != (replicated_guild.owner_id, replicated_guild.owner_domain):
                raise ValueError("guild access revocation was not signed for the guild owner")
            target = envelope.content.get("target")
            if not isinstance(target, dict):
                raise ValueError("guild access revocation target is invalid")
            revocation_reason = envelope.content.get("reason")
            if revocation_reason not in {
                "member_left",
                "member_kicked",
                "member_banned",
            }:
                raise ValueError("guild access revocation reason is invalid")
            access_revocation_target = (
                database_snowflake(target.get("id"), "guild access revocation target id"),
                normalize_domain(str(target.get("domain", ""))),
            )
            await apply_guild_access_revocation(
                session,
                settings,
                replicated_guild,
                user_id=access_revocation_target[0],
                user_domain=access_revocation_target[1],
            )
        elif (
            envelope.type == "guild.instance_access.revoked"
            and terminal_room_ref is not None
            and await session.get(Guild, (terminal_room_ref[1], terminal_room_ref[2])) is None
        ):
            if envelope.content.get("target_domain") != settings.domain:
                raise ValueError("terminal guild deletion targeted another instance")
            (
                missing_guild_local_purges,
                missing_guild_remote_purges,
                _missing_guild_destinations,
                missing_guild_wakes,
            ) = await prepare_terminal_room_media_by_ref(
                session,
                settings,
                room_kind="guild",
                room_id=terminal_room_ref[1],
                room_domain=terminal_room_ref[2],
            )
            local_media_purge_refs.extend(missing_guild_local_purges)
            remote_media_purge_refs.extend(missing_guild_remote_purges)
            delivery_wakes.update(missing_guild_wakes)
        elif envelope.type == "guild.instance_access.revoked":
            guild_id = database_snowflake(envelope.context.get("guild_id"), "guild id")
            guild_domain = normalize_domain(str(envelope.context.get("guild_domain", "")))
            if guild_domain != envelope.origin:
                raise ValueError("guild instance revocation did not originate at the guild home")
            replicated_guild = await session.get(Guild, (guild_id, guild_domain))
            if replicated_guild is None:
                raise ValueError("guild instance revocation references an unknown replica")
            # Terminal deletion is authenticated by the exact origin-signed
            # control. The retained owner can legitimately be stale after a
            # transfer while this instance no longer has snapshot access.
            if terminal_room_ref is None and (
                database_snowflake(envelope.actor.id, "guild instance revocation actor id"),
                envelope.actor.domain,
            ) != (replicated_guild.owner_id, replicated_guild.owner_domain):
                raise ValueError("guild instance revocation was not signed for the guild owner")
            revocation_reason = envelope.content.get("reason")
            target_domain = normalize_domain(str(envelope.content.get("target_domain", "")))
            if revocation_reason == "guild_deleted":
                if terminal_room_ref is None or target_domain != settings.domain:
                    raise ValueError("terminal guild deletion target is invalid")
                (
                    terminal_guild_purges,
                    _terminal_guild_destinations,
                    terminal_guild_wakes,
                ) = await prepare_terminal_guild_media(session, settings, replicated_guild)
                local_media_purge_refs.extend(terminal_guild_purges)
                delivery_wakes.update(terminal_guild_wakes)
            elif revocation_reason not in {
                "instance_banned",
                "instance_suspended",
                "instance_silenced",
                "instance_blocked",
            }:
                raise ValueError("guild instance revocation reason is invalid")
            instance_access_revoked_users = await apply_guild_instance_access_revocation(
                session,
                settings,
                replicated_guild,
                target_domain=target_domain,
            )
        elif envelope.type == "guild.resync.required":
            guild_id = database_snowflake(envelope.context.get("guild_id"), "guild id")
            guild_domain = str(envelope.context.get("guild_domain"))
            if guild_domain != envelope.origin:
                raise ValueError("guild resync marker did not originate at the guild home")
            replicated_guild = await session.get(Guild, (guild_id, guild_domain))
            if replicated_guild is None:
                raise ValueError("guild resync marker references an unknown replica")
            if (
                database_snowflake(envelope.actor.id, "guild resync actor id"),
                envelope.actor.domain,
            ) != (replicated_guild.owner_id, replicated_guild.owner_domain):
                raise ValueError("guild resync marker was not signed for the guild owner")
            required_snapshot_seq = database_snowflake(
                envelope.context.get("snapshot_seq"), "required snapshot sequence"
            )
            if replicated_guild.last_event_seq < required_snapshot_seq:
                raise FederationResyncRetry
        elif envelope.type == "guild.proxy.message.create":
            actor_raw = envelope.content["actor"]
            if (
                str(actor_raw.get("id")) != envelope.actor.id
                or actor_raw.get("origin_domain") != envelope.actor.domain
            ):
                raise ValueError("proxy actor mismatch")
            actor = await upsert_remote_user(
                session, settings, RemoteUserProfile.model_validate(actor_raw)
            )
            await require_remote_user_creation_allowed(session, actor)
            if str(envelope.context.get("guild_domain")) != settings.domain:
                raise ValueError("proxy write was not addressed to this guild authority")
            guild = await home_guild(
                session,
                settings,
                database_snowflake(envelope.context.get("guild_id"), "guild id"),
                for_update=True,
            )
            loaded_proxy_channel = await session.get(
                Channel,
                (
                    database_snowflake(envelope.content.get("channel_id"), "channel id"),
                    guild.origin_domain,
                ),
            )
            if (
                loaded_proxy_channel is None
                or loaded_proxy_channel.guild_id != guild.id
                or loaded_proxy_channel.type not in {0, 5, 10, 11, 12}
            ):
                raise ValueError("proxy channel is not in the guild")
            channel = loaded_proxy_channel
            raw_attachments = envelope.content.get("attachments", [])
            if not isinstance(raw_attachments, list) or len(raw_attachments) > 10:
                raise ValueError("proxy write attachment list is invalid")
            needed = Permission.VIEW_CHANNEL | (
                Permission.SEND_MESSAGES_IN_THREADS
                if channel.type in {10, 11, 12}
                else Permission.SEND_MESSAGES
            )
            if raw_attachments:
                needed |= Permission.ATTACH_FILES
            actor_permissions = await require_permissions(
                session,
                redis,
                guild,
                actor,
                needed,
                channel=channel,
            )
            if (
                channel.type in {10, 11, 12}
                and channel.locked
                and not actor_permissions & Permission.MANAGE_THREADS
            ):
                raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})
            if (
                channel.type in {10, 11, 12}
                and actor.account_type == "bot"
                and (channel.e2ee_required or channel.encryption_mode == "e2ee")
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "BOT_THREAD_E2EE_UNSUPPORTED"},
                )
            await validate_custom_emoji_use(
                session,
                actor,
                envelope.content.get("content"),
                target_guild=guild,
                target_permissions=actor_permissions,
                trust_unknown_external=True,
            )
            nonce = str(envelope.content["client_nonce"])
            if not 1 <= len(nonce) <= 64:
                raise ValueError("proxy write client nonce is invalid")
            await lock_proxy_nonce(session, guild, actor, channel, nonce)
            proxy_content = envelope.content.get("content")
            proxy_e2ee = validate_e2ee_envelope(envelope.content.get("e2ee"))
            if proxy_e2ee is not None and (
                proxy_e2ee.get("version") != 2
                or proxy_e2ee.get("operation") != "create"
                or "target_message" in proxy_e2ee
            ):
                raise ValueError("proxy encrypted write operation is invalid")
            if proxy_content is not None and (
                not isinstance(proxy_content, str) or not 1 <= len(proxy_content) <= 4000
            ):
                raise ValueError("proxy write content is invalid")
            if proxy_content is not None and proxy_e2ee is not None:
                raise ValueError("proxy write mixes plaintext and encrypted content")
            if proxy_content is None and proxy_e2ee is None and not raw_attachments:
                raise ValueError(
                    "proxy write requires content, encrypted content, or an attachment"
                )
            require_message_encryption_policy(
                channel,
                content=proxy_content,
                e2ee=proxy_e2ee,
                attachment_count=len(raw_attachments),
            )
            raw_mention_refs = envelope.content.get("mention_user_refs", [])
            if not isinstance(raw_mention_refs, list) or len(raw_mention_refs) > 5_000:
                raise ValueError("proxy write mention list is invalid")
            parsed_mention_refs: list[tuple[int, str]] = []
            for ref in raw_mention_refs:
                if not isinstance(ref, dict):
                    raise ValueError("proxy write mention reference is invalid")
                parsed_mention_refs.append(
                    (
                        database_snowflake(ref.get("id"), "mentioned user id"),
                        str(ref.get("origin_domain")),
                    )
                )
            role_mention_refs = set(
                await role_mention_recipients(session, guild, proxy_content, actor_permissions)
            )
            parsed_mention_refs = merge_mention_recipients(
                parsed_mention_refs,
                list(role_mention_refs),
            )
            mention_refs = await validated_guild_mentions(session, guild, parsed_mention_refs)
            raw_reference = envelope.content.get("referenced_message_ref")
            referenced_message: Message | None = None
            if raw_reference is not None:
                if not isinstance(raw_reference, dict):
                    raise ValueError("proxy write message reference is invalid")
                reference_id = database_snowflake(raw_reference.get("id"), "referenced message id")
                reference_domain = normalize_domain(str(raw_reference.get("origin_domain", "")))
                referenced_message = await session.get(Message, (reference_id, reference_domain))
                if referenced_message is None or (
                    referenced_message.channel_id,
                    referenced_message.channel_domain,
                ) != (channel.id, channel.origin_domain):
                    raise ValueError("proxy write references a message outside the channel")
            home_message = await session.scalar(
                select(Message).where(
                    Message.channel_id == channel.id,
                    Message.channel_domain == channel.origin_domain,
                    Message.author_id == actor.id,
                    Message.author_domain == actor.origin_domain,
                    Message.client_nonce == nonce,
                )
            )
            if home_message is None:
                prior_thread_message_projection = (
                    capture_thread_message_projection(channel)
                    if channel.type in {10, 11, 12}
                    else None
                )
                thread_was_unarchived = False
                if channel.type in {10, 11, 12} and channel.archived:
                    if channel.locked and not actor_permissions & Permission.MANAGE_THREADS:
                        raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})
                    await require_active_thread_capacity(
                        session,
                        guild,
                        excluding=(channel.id, channel.origin_domain),
                    )
                    channel.archived = False
                    channel.archive_timestamp = datetime.now(UTC)
                    thread_was_unarchived = True
                if (
                    channel.rate_limit_per_user
                    and actor.account_type != "bot"
                    and not actor_permissions & Permission.BYPASS_SLOWMODE
                ):
                    allowed = await redis.set(
                        (
                            f"slowmode:{channel.origin_domain}:{channel.id}:"
                            f"{actor.origin_domain}:{actor.id}"
                        ),
                        "1",
                        ex=channel.rate_limit_per_user,
                        nx=True,
                    )
                    if not allowed:
                        slowmode_key = (
                            f"slowmode:{channel.origin_domain}:{channel.id}:"
                            f"{actor.origin_domain}:{actor.id}"
                        )
                        raise HTTPException(
                            status_code=429,
                            detail={
                                "code": "SLOWMODE_RATE_LIMITED",
                                "retry_after_ms": max(1000, await redis.pttl(slowmode_key)),
                            },
                        )
                home_message = Message(
                    id=await snowflake.mint(),
                    origin_domain=settings.domain,
                    channel_id=channel.id,
                    channel_domain=channel.origin_domain,
                    author_id=actor.id,
                    author_domain=actor.origin_domain,
                    content=proxy_content,
                    e2ee=proxy_e2ee,
                    encryption_policy_generation=channel.encryption_policy_generation,
                    encryption_epoch=channel.encryption_epoch,
                    client_nonce=nonce,
                    referenced_message_id=(
                        referenced_message.id if referenced_message is not None else None
                    ),
                    referenced_message_domain=(
                        referenced_message.origin_domain if referenced_message is not None else None
                    ),
                    mention_user_refs=mention_refs,
                    flags=(0 if actor_permissions & Permission.EMBED_LINKS else 4),
                )
                session.add(home_message)
                await session.flush()
                if channel.type in {10, 11, 12}:
                    channel.last_message_id = home_message.id
                    channel.last_message_domain = home_message.origin_domain
                    channel.message_count = int(channel.message_count or 0) + 1
                    channel.total_message_sent = int(channel.total_message_sent or 0) + 1
                    channel.last_activity_at = home_message.created_at
                    (
                        home_thread_members_added,
                        thread_rekeyed,
                        failed_role_mentions,
                    ) = await admit_thread_message_members(
                        session,
                        redis,
                        settings,
                        guild,
                        channel,
                        actor,
                        actor_permissions,
                        parsed_mention_refs,
                        role_mention_refs,
                    )
                    if failed_role_mentions:
                        home_message.flags |= MESSAGE_FLAG_FAILED_TO_MENTION_SOME_ROLES_IN_THREAD
                    home_thread = channel
                    # Activity/count projections change on every real reply.
                    home_thread_updated = True
                    home_thread_was_unarchived = thread_was_unarchived
                    if thread_was_unarchived or thread_rekeyed:
                        if prior_thread_message_projection is None:
                            raise RuntimeError("thread message projection was not captured")
                        await queue_guild_mutation(
                            session,
                            settings,
                            guild,
                            actor,
                            "guild.channel.update",
                            {
                                "channel": thread_structural_state_before_message(
                                    channel,
                                    prior_thread_message_projection,
                                )
                            },
                            channel=channel,
                        )
                session.add(
                    MessageProjection(
                        message_id=home_message.id,
                        message_domain=home_message.origin_domain,
                        channel_id=home_message.channel_id,
                        channel_domain=home_message.channel_domain,
                        mention_user_refs=mention_refs,
                    )
                )
                await advance_channel_cursor(
                    session,
                    channel,
                    home_message.id,
                    home_message.origin_domain,
                )
                home_message_created = True
            home_message_attachments = await replicate_message_attachments(
                session, settings, home_message, actor, raw_attachments
            )
            existing_event = await guild_event_for_message(session, guild, home_message)
            if existing_event is None:
                seq = await assign_guild_sequence(session, guild)
                owner = await session.get(User, (guild.owner_id, guild.owner_domain))
                if owner is None or owner.origin_domain != settings.domain:
                    raise RuntimeError("guild owner cannot sign the commit event")
                committed = await build_envelope(
                    session,
                    settings,
                    "guild.message.committed",
                    owner,
                    {
                        "message": message_payload(home_message, actor, home_message_attachments),
                        "author": profile_from_user(actor),
                        "thread_starter": False,
                    },
                    context={
                        "guild_id": str(guild.id),
                        "guild_domain": guild.origin_domain,
                        "seq": str(seq),
                    },
                )
                store_guild_event(session, guild, seq, str(committed["event_id"]), committed)
            else:
                committed = existing_event.envelope
            proxy_destinations = await remote_destinations_with_channel_access(
                session, settings, guild, channel
            )
            for destination in proxy_destinations:
                await queue_event(session, settings, destination, committed)
            delivery_wakes.update(proxy_destinations)
        elif envelope.type == "message.send_rejected":
            if str(envelope.context.get("guild_domain")) != envelope.origin:
                raise ValueError("write rejection did not originate at the guild home")
            rejection_guild = await session.get(
                Guild,
                (int(envelope.context["guild_id"]), envelope.origin),
            )
            if rejection_guild is None:
                raise ValueError("write rejection references an unknown guild")
            if (
                database_snowflake(envelope.actor.id, "write rejection actor id"),
                envelope.actor.domain,
            ) != (rejection_guild.owner_id, rejection_guild.owner_domain):
                raise ValueError("write rejection was not signed for the guild owner")
            target = envelope.content["target"]
            rejection_target = (
                database_snowflake(target.get("id"), "write rejection target id"),
                str(target.get("domain")),
            )
            if rejection_target[1] != settings.domain:
                raise ValueError("write rejection target is not local")
            target_member = await session.get(
                GuildMember,
                (
                    rejection_guild.id,
                    rejection_guild.origin_domain,
                    rejection_target[0],
                    rejection_target[1],
                ),
            )
            rejected_channel = await session.get(
                Channel,
                (
                    database_snowflake(envelope.content.get("channel_id"), "rejected channel id"),
                    envelope.origin,
                ),
            )
            if (
                target_member is None
                or rejected_channel is None
                or (
                    rejected_channel.guild_id,
                    rejected_channel.guild_domain,
                )
                != (rejection_guild.id, rejection_guild.origin_domain)
            ):
                raise ValueError("write rejection is not bound to a local guild member")
            target_user = await session.get(User, rejection_target)
            client_nonce = str(envelope.content["client_nonce"])
            if target_user is None or not await has_outbound_guild_proxy(
                session,
                envelope.origin,
                rejection_guild.id,
                rejected_channel.id,
                client_nonce,
                target_user,
            ):
                raise ValueError("write rejection has no matching queued local write")
            rejection_code = str(envelope.content["code"])
            if not 1 <= len(rejection_code) <= 64:
                raise ValueError("write rejection code is invalid")
            timeout_reason = envelope.content.get("reason")
            timeout_until = envelope.content.get("timeout_until")
            timeout_indefinite = envelope.content.get("timeout_indefinite", False)
            if rejection_code == "MEMBER_TIMED_OUT":
                timeout_reason = validated_rejection_timeout_reason(timeout_reason)
                if timeout_until is not None:
                    parsed_timeout = datetime.fromisoformat(str(timeout_until))
                    if parsed_timeout.tzinfo is None:
                        raise ValueError("write rejection timeout lacks a timezone")
                if not isinstance(timeout_indefinite, bool):
                    raise ValueError("write rejection timeout mode is invalid")
            else:
                timeout_reason = None
                timeout_until = None
                timeout_indefinite = False
            rejection_payload = {
                "channel_id": str(envelope.content["channel_id"]),
                "channel_domain": envelope.origin,
                "client_nonce": client_nonce,
                "code": rejection_code,
                "reason": timeout_reason,
                "timeout_until": timeout_until,
                "timeout_indefinite": timeout_indefinite,
            }
        elif envelope.type == "media.delete":
            if media_attachment_ref is None or media_signing_key_id is None:
                raise RuntimeError("validated media tombstone state disappeared")
            attachment_number, attachment_origin = media_attachment_ref
            # Validate the signed generation before accepting this as terminal
            # truth. The retained set is ordered by signed generation rather
            # than receive time so a delayed pre-rotation event cannot replace
            # or be relayed ahead of a newer proof.
            media_delete_order(serialized_envelope)
            retained_proofs = await locked_retained_media_delete_events(
                session,
                attachment_number,
                attachment_origin,
            )
            if not retained_proofs:
                raise RuntimeError("verified media tombstone was not retained")
            selected_proof = retained_proofs[0]
            selected_envelope = selected_proof.envelope
            incoming_is_selected = selected_proof.event_id == envelope.event_id
            if not incoming_is_selected:
                raise RuntimeError("new media tombstone generation was not selected")
            selected_generation = media_delete_generation(selected_envelope)
            signer_number = database_snowflake(envelope.actor.id, "media tombstone actor id")
            remote_attachment = await session.get(
                Attachment, (attachment_number, attachment_origin)
            )
            existing_tombstone = await session.get(
                RemoteMediaTombstone,
                (attachment_origin, attachment_number),
            )
            remote_cache_exists = (
                await session.scalar(
                    select(RemoteMediaCache.attachment_id)
                    .where(
                        RemoteMediaCache.origin_domain == attachment_origin,
                        RemoteMediaCache.attachment_id == attachment_number,
                    )
                    .limit(1)
                )
                is not None
            )
            create_fast_tombstone = (
                existing_tombstone is not None
                or existing_source is None
                or remote_attachment is not None
                or remote_cache_exists
            )
            if existing_tombstone is None and existing_source is None:
                retained_tombstones = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(MediaTombstoneSource)
                        .where(MediaTombstoneSource.attachment_domain == attachment_origin)
                    )
                    or 0
                )
                if retained_tombstones >= settings.federation_remote_media_tombstones_per_origin:
                    raise RemoteMediaTombstoneQuotaExceeded("remote media tombstone quota exceeded")
            # Keep a bounded tombstone even if the corresponding create has
            # not arrived yet. This preserves delete-before-create ordering
            # without allowing permanent attacker-selected state: admission is
            # per origin and retention expires the row.
            if create_fast_tombstone:
                await session.execute(
                    pg_insert(RemoteMediaTombstone)
                    .values(
                        origin_domain=attachment_origin,
                        attachment_id=attachment_number,
                        event_id=selected_proof.event_id,
                        expires_at=datetime.now(UTC)
                        + timedelta(days=settings.federation_event_retention_days),
                    )
                    .on_conflict_do_update(
                        index_elements=["origin_domain", "attachment_id"],
                        set_={
                            "event_id": selected_proof.event_id,
                            "deleted_at": datetime.now(UTC),
                            "expires_at": datetime.now(UTC)
                            + timedelta(days=settings.federation_event_retention_days),
                        },
                    )
                )
            if remote_attachment is not None:
                remote_attachment.deleted_at = datetime.now(UTC)
                if (
                    remote_attachment.message_id is not None
                    and remote_attachment.message_domain is not None
                ):
                    remote_message = await session.get(
                        Message,
                        (
                            remote_attachment.message_id,
                            remote_attachment.message_domain,
                        ),
                    )
                    if remote_message is not None:
                        remote_channel = await session.get(
                            Channel,
                            (remote_message.channel_id, remote_message.channel_domain),
                        )
                        if remote_channel is not None:
                            media_tombstone_dispatch_payload = terminal_attachment_update_payload(
                                remote_attachment,
                                message_id=remote_message.id,
                                message_domain=remote_message.origin_domain,
                                channel_id=remote_channel.id,
                                channel_domain=remote_channel.origin_domain,
                            )
                            media_tombstone_channel_ref = (
                                remote_channel.id,
                                remote_channel.origin_domain,
                            )
                            if (
                                remote_channel.guild_id is not None
                                and remote_channel.guild_domain is not None
                            ):
                                media_tombstone_guild_ref = (
                                    remote_channel.guild_id,
                                    remote_channel.guild_domain,
                                )
            # The cache-budget fence was acquired before this attachment's
            # media fence during preflight and remains held through commit.
            # Expire every admitted variant transactionally: a cache writer
            # that won the lock race is invalidated here, while a later writer
            # waits and observes the durable source inserted below.
            await session.execute(
                update(RemoteMediaCache)
                .where(
                    RemoteMediaCache.origin_domain == attachment_origin,
                    RemoteMediaCache.attachment_id == attachment_number,
                )
                .values(expires_at=datetime.now(UTC))
            )
            await session.execute(
                pg_insert(MediaTombstoneSource)
                .values(
                    attachment_id=attachment_number,
                    attachment_domain=attachment_origin,
                    signer_id=signer_number,
                    signer_domain=envelope.actor.domain,
                    event_id=selected_proof.event_id,
                    key_id=media_signing_key_id,
                    generation=selected_generation,
                    updated_at=datetime.now(UTC),
                )
                .on_conflict_do_update(
                    index_elements=["attachment_id", "attachment_domain"],
                    set_={
                        "signer_id": signer_number,
                        "signer_domain": envelope.actor.domain,
                        "event_id": selected_proof.event_id,
                        "key_id": media_signing_key_id,
                        "generation": selected_generation,
                        "updated_at": datetime.now(UTC),
                    },
                )
            )
            historical_recipients = await historical_attachment_destinations_by_ref(
                session,
                attachment_number,
                attachment_origin,
            )
            historical_recipients.difference_update(
                {settings.domain, envelope.origin, principal.origin}
            )
            for destination in sorted(historical_recipients):
                await queue_event(
                    session,
                    settings,
                    destination,
                    selected_envelope,
                )
            await record_media_tombstone_destinations(
                session,
                attachment_number,
                attachment_origin,
                historical_recipients,
            )
            delivery_wakes.update(historical_recipients)
            # Committing the local tombstone and relay outboxes is only the
            # first half of transitive acknowledgement. Keep the immediate
            # upstream retrying until every pre-existing downstream route has
            # accepted this exact selected generation.
            media_delete_cascade_pending = bool(historical_recipients)
            # A newly selected generation makes every older pending relay
            # permanently obsolete. Marking them explicitly prevents a stale
            # key proof from retrying forever after the current proof arrived.
            stale_proofs = [
                proof for proof in retained_proofs[1:] if proof.event_id != selected_proof.event_id
            ]
            stale_proof_ids = [proof.event_id for proof in stale_proofs]
            if stale_proof_ids:
                compacted_bytes = sum(
                    proof.envelope_bytes
                    for proof in stale_proofs
                    if not (inserted_event is not None and proof.event_id == envelope.event_id)
                )
                peer.federation_inbox_event_bytes = max(
                    0,
                    peer.federation_inbox_event_bytes - compacted_bytes,
                )
                global_ledger.federation_inbox_event_bytes = max(
                    0,
                    global_ledger.federation_inbox_event_bytes - compacted_bytes,
                )
                stale_inbox_ids = list(
                    await session.scalars(
                        select(FederationInbox.event_id).where(
                            FederationInbox.origin_domain == attachment_origin,
                            FederationInbox.event_id.in_(stale_proof_ids),
                        )
                    )
                )
                if stale_inbox_ids:
                    peer.federation_inbox_events = max(
                        0,
                        peer.federation_inbox_events - len(stale_inbox_ids),
                    )
                    global_ledger.federation_inbox_events = max(
                        0,
                        global_ledger.federation_inbox_events - len(stale_inbox_ids),
                    )
                    await session.execute(
                        delete(FederationInbox).where(
                            FederationInbox.origin_domain == attachment_origin,
                            FederationInbox.event_id.in_(stale_inbox_ids),
                        )
                    )
                await session.execute(
                    delete(FederationEvent).where(
                        FederationEvent.origin_domain == attachment_origin,
                        FederationEvent.event_id.in_(stale_proof_ids),
                    )
                )
                if not incoming_is_selected:
                    # The just-inserted stale proof was compacted before it
                    # became durable, so it must not be added to retained-event
                    # quota accounting after commit.
                    inserted_event = None
            media_purge_target = (attachment_origin, attachment_number)
        else:
            raise ValueError("unsupported event type")
        if replicated_guild is not None and replicated_guild not in session.deleted:
            await admit_replica_storage(session, settings, replicated_guild)
        if terminal_room_ref is not None:
            if terminal_room_incoming_generation is None or terminal_room_signing_key_id is None:
                raise RuntimeError("terminal room verification state disappeared")
            room_kind, room_id, room_domain = terminal_room_ref
            now = datetime.now(UTC)
            base_content = terminal_room_base_content(serialized_envelope)
            actor_id = database_snowflake(envelope.actor.id, "terminal room actor id")
            if terminal_room_receipt is None:
                terminal_room_receipt = TerminalRoomDeletion(
                    room_kind=room_kind,
                    room_id=room_id,
                    room_domain=room_domain,
                    destination_domain=settings.domain,
                    actor_id=actor_id,
                    actor_domain=envelope.actor.domain,
                    event_type=envelope.type,
                    content=base_content,
                    context=envelope.context,
                    event_id=envelope.event_id,
                    key_id=terminal_room_signing_key_id,
                    generation=terminal_room_incoming_generation,
                    acknowledged_at=now,
                    updated_at=now,
                )
                session.add(terminal_room_receipt)
            else:
                terminal_room_receipt.event_id = envelope.event_id
                terminal_room_receipt.key_id = terminal_room_signing_key_id
                terminal_room_receipt.generation = terminal_room_incoming_generation
                terminal_room_receipt.acknowledged_at = now
                terminal_room_receipt.updated_at = now
            if terminal_room_replacement_event is not None:
                peer.federation_inbox_event_bytes = max(
                    0,
                    peer.federation_inbox_event_bytes
                    - terminal_room_replacement_event.envelope_bytes,
                )
                global_ledger.federation_inbox_event_bytes = max(
                    0,
                    global_ledger.federation_inbox_event_bytes
                    - terminal_room_replacement_event.envelope_bytes,
                )
            if terminal_room_replacement_inbox is not None:
                peer.federation_inbox_events = max(0, peer.federation_inbox_events - 1)
                global_ledger.federation_inbox_events = max(
                    0, global_ledger.federation_inbox_events - 1
                )
                await session.delete(terminal_room_replacement_inbox)
            if terminal_room_replacement_event is not None:
                await session.delete(terminal_room_replacement_event)
        inbox.status = "processed"
        inbox.result_code = None
        inbox.processed_at = datetime.now(UTC)
        await event_work.commit()
        if inserted_event is not None:
            peer.federation_inbox_event_bytes += envelope_bytes
            global_ledger.federation_inbox_event_bytes += envelope_bytes
        await session.commit()
        durably_committed = True
        if media_tombstone_dispatch_payload is not None and media_tombstone_channel_ref is not None:
            if media_tombstone_guild_ref is not None:
                await publish_dispatch(
                    redis,
                    guild_topic(media_tombstone_guild_ref[1], media_tombstone_guild_ref[0]),
                    "ATTACHMENT_UPDATE",
                    media_tombstone_dispatch_payload,
                )
            else:
                local_participants = await session.execute(
                    select(DMParticipant.user_id, DMParticipant.user_domain).where(
                        DMParticipant.conversation_id == media_tombstone_channel_ref[0],
                        DMParticipant.conversation_domain == media_tombstone_channel_ref[1],
                        DMParticipant.user_domain == settings.domain,
                    )
                )
                for user_id, user_domain in local_participants:
                    await publish_dispatch(
                        redis,
                        user_topic(user_domain, user_id),
                        "ATTACHMENT_UPDATE",
                        media_tombstone_dispatch_payload,
                    )
        if replicated_message is not None:
            await publish_replicated_dm_message(session, redis, settings, replicated_message)
            await enqueue_best_effort(
                mentions_fanout,
                replicated_message.id,
                replicated_message.origin_domain,
            )
        if replicated_group_call is not None:
            call_identities = set(cast(list[str], replicated_group_call["participants"]))
            if await create_call(
                redis,
                replicated_group_call,
                call_identities,
                settings,
                accepted={str(replicated_group_call["caller"])},
            ):
                for identity in sorted(call_identities):
                    user_id, user_domain = parse_participant_identity(identity)
                    if user_domain == settings.domain:
                        await publish_dispatch(
                            redis,
                            user_topic(user_domain, user_id),
                            "CALL_RING",
                            replicated_group_call,
                        )
        if created_dm_channel is not None and dm_channel_recipient is not None:
            # The durable commit expires ORM attributes. Refresh before
            # rendering the best-effort gateway projection so payload helpers
            # never trigger asynchronous lazy IO outside a greenlet context.
            await session.refresh(created_dm_channel)
            participants = list(
                await session.scalars(
                    select(User)
                    .join(
                        DMParticipant,
                        (DMParticipant.user_id == User.id)
                        & (DMParticipant.user_domain == User.origin_domain),
                    )
                    .where(
                        DMParticipant.conversation_id == created_dm_channel.id,
                        DMParticipant.conversation_domain == created_dm_channel.origin_domain,
                    )
                )
            )
            await publish_dispatch(
                redis,
                user_topic(settings.domain, dm_channel_recipient.id),
                "CHANNEL_CREATE",
                dm_channel_payload(
                    created_dm_channel,
                    [
                        participant
                        for participant in participants
                        if (participant.id, participant.origin_domain)
                        != (dm_channel_recipient.id, dm_channel_recipient.origin_domain)
                    ],
                    conversation=await session.get(
                        DMConversation,
                        (created_dm_channel.id, created_dm_channel.origin_domain),
                    ),
                ),
            )
        if (
            created_dm_channel is not None
            and group_state_conversation is not None
            and group_state_changed
            and group_state_ref is not None
        ):
            (
                group_state_conversation,
                created_dm_channel,
                group_state_after,
            ) = await reload_group_projection(session, *group_state_ref)
            group_history = dm_history_metadata(
                group_state_conversation,
                local_domain=settings.domain,
                remote_available=await dm_authority_history_available(
                    session,
                    group_state_conversation,
                    local_domain=settings.domain,
                ),
            )
            before_refs = group_state_before_refs
            after_refs = {(user.id, user.origin_domain) for user in group_state_after}
            for user in group_state_after:
                if user.origin_domain != settings.domain or not user.is_local:
                    continue
                await publish_dispatch(
                    redis,
                    user_topic(settings.domain, user.id),
                    (
                        "CHANNEL_CREATE"
                        if (user.id, user.origin_domain) not in before_refs
                        else "CHANNEL_UPDATE"
                    ),
                    dm_channel_payload(
                        created_dm_channel,
                        [
                            item
                            for item in group_state_after
                            if (item.id, item.origin_domain) != (user.id, user.origin_domain)
                        ],
                        conversation=group_state_conversation,
                        history=group_history,
                    ),
                )
            for user_id, user_domain in before_refs - after_refs:
                if user_domain == settings.domain:
                    await publish_dispatch(
                        redis,
                        user_topic(settings.domain, user_id),
                        "CHANNEL_DELETE",
                        {
                            "id": str(created_dm_channel.id),
                            "origin_domain": created_dm_channel.origin_domain,
                        },
                    )
        # Newly added users need the conversation projection before its first
        # system message, otherwise clients can legitimately drop that message
        # because the channel is not known yet.
        if group_notice_ref is not None:
            group_notice_message = await session.get(
                Message, group_notice_ref, populate_existing=True
            )
            if group_notice_message is None:
                raise RuntimeError("committed group DM notice disappeared")
            await publish_replicated_dm_message(session, redis, settings, group_notice_message)
        if (
            relationship_application is not None
            and relationship_application.relation_type is not None
        ):
            await publish_dispatch(
                redis,
                user_topic(
                    relationship_application.recipient.origin_domain,
                    relationship_application.recipient.id,
                ),
                "USER_UPDATE",
                {
                    "relationship": {
                        "type": relationship_application.relation_type,
                        "user": user_payload(relationship_application.actor),
                    }
                },
            )
        if replicated_guild_message is not None and replicated_guild is not None:
            if replicated_thread_message is not None:
                await publish_dispatch(
                    redis,
                    guild_topic(replicated_guild.origin_domain, replicated_guild.id),
                    "THREAD_UPDATE",
                    channel_payload(replicated_thread_message),
                )
            await publish_dispatch(
                redis,
                guild_topic(replicated_guild.origin_domain, replicated_guild.id),
                "MESSAGE_CREATE",
                await render_message_payload(session, replicated_guild_message),
            )
            await enqueue_best_effort(
                mentions_fanout,
                replicated_guild_message.id,
                replicated_guild_message.origin_domain,
            )
        if replicated_guild_member is not None and replicated_guild is not None:
            await publish_dispatch(
                redis,
                guild_topic(replicated_guild.origin_domain, replicated_guild.id),
                "GUILD_MEMBER_ADD",
                {
                    "guild_id": str(replicated_guild.id),
                    "user": user_payload(replicated_guild_member),
                },
            )
        if authoritative_leave_guild is not None and authoritative_leave_target is not None:
            await wake_queued_guild_federation(authoritative_leave_guild)
            await publish_deleted_installation_roles(
                redis,
                authoritative_leave_guild,
                authoritative_leave_role_refs,
            )
            await publish_guild_thread_member_cleanup(
                redis,
                authoritative_leave_guild,
                authoritative_leave_thread_removals,
            )
            await publish_dispatch(
                redis,
                guild_topic(authoritative_leave_guild.origin_domain, authoritative_leave_guild.id),
                "GUILD_MEMBER_REMOVE",
                {
                    "guild_id": str(authoritative_leave_guild.id),
                    "guild_domain": authoritative_leave_guild.origin_domain,
                    "user_id": str(authoritative_leave_target[0]),
                    "user_domain": authoritative_leave_target[1],
                },
            )
        if replicated_guild_dispatch is not None and replicated_guild is not None:
            dispatch_type, dispatch_payload = replicated_guild_dispatch
            await publish_dispatch(
                redis,
                guild_topic(replicated_guild.origin_domain, replicated_guild.id),
                dispatch_type,
                dispatch_payload,
            )
        if history_access_changed and replicated_guild is not None:
            await enqueue_best_effort(
                federation_history_sync_guild,
                replicated_guild.id,
                replicated_guild.origin_domain,
            )
            history_channel_ids = list(
                await session.scalars(
                    select(Channel.id).where(
                        Channel.guild_id == replicated_guild.id,
                        Channel.guild_domain == replicated_guild.origin_domain,
                    )
                )
            )
            if history_channel_ids:
                await redis.delete(
                    *(
                        f"channel:last_message:{replicated_guild.origin_domain}:{channel_id}"
                        for channel_id in history_channel_ids
                    )
                )
        if access_revocation_target is not None and replicated_guild is not None:
            await publish_dispatch(
                redis,
                user_topic(access_revocation_target[1], access_revocation_target[0]),
                "GUILD_DELETE",
                {
                    "id": str(replicated_guild.id),
                    "origin_domain": replicated_guild.origin_domain,
                },
            )
        if instance_access_revoked_users and replicated_guild is not None:
            for revoked_user_id in instance_access_revoked_users:
                await publish_dispatch(
                    redis,
                    user_topic(settings.domain, revoked_user_id),
                    "GUILD_DELETE",
                    {
                        "id": str(replicated_guild.id),
                        "origin_domain": replicated_guild.origin_domain,
                    },
                )
        if home_thread is not None:
            if home_thread.guild_domain is None or home_thread.guild_id is None:
                raise RuntimeError("thread lost its guild reference after commit")
            thread_topic = guild_topic(home_thread.guild_domain, int(home_thread.guild_id))
            if home_thread_updated:
                await publish_dispatch(
                    redis,
                    thread_topic,
                    "THREAD_UPDATE",
                    channel_payload(home_thread),
                )
            if home_thread_was_unarchived:
                home_thread_guild = await session.get(
                    Guild,
                    (home_thread.guild_id, home_thread.guild_domain),
                )
                if home_thread_guild is None:
                    raise RuntimeError("thread guild disappeared after unarchive")
                await publish_current_thread_member_updates(
                    session,
                    redis,
                    home_thread_guild,
                    home_thread,
                )
            if home_thread_members_added:
                rendered_members = [
                    thread_member_payload(member) for member in home_thread_members_added
                ]
                rich_rendered_members = [
                    await rich_thread_member_payload(session, member)
                    for member in home_thread_members_added
                ]
                for rendered_member in rendered_members:
                    target_ref = f"{rendered_member['user_id']}@{rendered_member['user_domain']}"
                    await publish_dispatch(
                        redis,
                        thread_topic,
                        "THREAD_CREATE",
                        channel_payload(home_thread) | {"member": rendered_member},
                        audience_user_refs=[target_ref],
                    )
                    await publish_dispatch(
                        redis,
                        thread_topic,
                        "THREAD_MEMBER_UPDATE",
                        rendered_member,
                        audience_user_refs=[target_ref],
                    )
                await publish_dispatch(
                    redis,
                    thread_topic,
                    "THREAD_MEMBERS_UPDATE",
                    {
                        "id": str(home_thread.id),
                        "thread_domain": home_thread.origin_domain,
                        "guild_id": str(home_thread.guild_id),
                        "guild_domain": home_thread.guild_domain,
                        "member_count": min(50, int(home_thread.member_count or 0)),
                        "added_members": rich_rendered_members,
                        "removed_member_ids": [],
                    },
                )
        if home_message is not None and home_message_created:
            await publish_dispatch(
                redis,
                guild_topic(home_message.channel_domain, int(envelope.context["guild_id"])),
                "MESSAGE_CREATE",
                message_payload(
                    home_message,
                    await session.get(User, (home_message.author_id, home_message.author_domain)),
                    home_message_attachments,
                ),
            )
            await enqueue_best_effort(mentions_fanout, home_message.id, home_message.origin_domain)
        if e2ee_policy_channels:
            await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
        for destination in delivery_wakes:
            await enqueue_best_effort(federation_deliver, destination)
        for attachment_id, attachment_domain in local_media_purge_refs:
            await enqueue_best_effort(
                media_local_purge,
                attachment_id,
                attachment_domain,
            )
        for attachment_domain, attachment_id in remote_media_purge_refs:
            await enqueue_best_effort(
                media_remote_purge,
                attachment_domain,
                attachment_id,
            )
        if remote_cache_gc_needed:
            await enqueue_best_effort(media_cache_gc)
        if rejection_target is not None and rejection_payload is not None:
            await publish_dispatch(
                redis,
                user_topic(rejection_target[1], rejection_target[0]),
                "MESSAGE_SEND_REJECTED",
                rejection_payload,
            )
        if dm_open_rejection_target is not None and dm_open_rejection_payload is not None:
            await publish_dispatch(
                redis,
                user_topic(dm_open_rejection_target[1], dm_open_rejection_target[0]),
                "DM_OPEN_REJECTED",
                dm_open_rejection_payload,
            )
        if media_purge_target is not None:
            await enqueue_best_effort(
                media_remote_purge,
                media_purge_target[0],
                media_purge_target[1],
            )
        return post_commit_inbox_result(
            envelope.event_id,
            media_delete_cascade_pending=media_delete_cascade_pending,
        )
    except Exception as exc:
        if durably_committed:
            # Redis fanout and Taskiq wakeups are best-effort projections after
            # the authoritative SQL transaction commits. Ordinary events stay
            # accepted; a media relay must still preserve its deliberate retry
            # until the committed child outboxes are acknowledged.
            await session.rollback()
            log.exception(
                "federation_post_commit_projection_failed",
                origin=envelope.origin,
                event_id=envelope.event_id,
                event_type=envelope.type,
            )
            return post_commit_inbox_result(
                envelope.event_id,
                media_delete_cascade_pending=media_delete_cascade_pending,
            )
        if not event_work.is_active:
            # The savepoint was released and the outer commit itself failed.
            # Nothing is known to be durable, so invite a clean replay instead
            # of manufacturing a terminal rejection in a broken transaction.
            await session.rollback()
            return InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code="KAED_FED_EVENT_RETRY",
            )
        await event_work.rollback()
        if isinstance(exc, FederationOutboxCapacityExceeded):
            # Nothing from this event is durable without its required outbound
            # follow-up. Remove the inbox claim so the sender can replay after
            # the bounded destination queue drains.
            inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
            if inbox is not None:
                await session.delete(inbox)
                peer.federation_inbox_events = max(0, peer.federation_inbox_events - 1)
                global_ledger.federation_inbox_events = max(
                    0, global_ledger.federation_inbox_events - 1
                )
            await session.commit()
            return InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code=exc.federation_code,
            )
        if isinstance(exc, RemoteMediaTombstoneQuotaExceeded):
            # Terminal media invalidation must not become a permanent reject
            # merely because this bounded replica cache is temporarily full.
            # Remove the replay claim and preserve exact quota accounting so
            # the sender can retry after retention or operator intervention.
            inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
            if inbox is not None:
                await session.delete(inbox)
                peer.federation_inbox_events = max(0, peer.federation_inbox_events - 1)
                global_ledger.federation_inbox_events = max(
                    0, global_ledger.federation_inbox_events - 1
                )
            await session.commit()
            return InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code=exc.federation_code,
            )
        if isinstance(exc, FederatedDMQuotaExceeded):
            # Replica capacity is recoverable: its rolling cache may converge
            # after projections/pins clear or an operator raises capacity, so
            # preserve replay by removing the inbox claim. Authoritative
            # history is user-owned and is never silently pruned; reject that
            # write deliberately with the stable capacity code.
            retryable = envelope.type == "dm.conversation.create"
            if envelope.type in {
                "dm.message.create",
                "dm.group.message.proposed",
                "dm.group.message.committed",
            }:
                raw_message = envelope.content.get("message")
                if isinstance(raw_message, dict):
                    try:
                        quota_conversation = await session.get(
                            DMConversation,
                            (
                                database_snowflake(
                                    raw_message.get("channel_id"), "DM quota channel id"
                                ),
                                normalize_domain(str(raw_message.get("channel_domain", ""))),
                            ),
                        )
                    except (FederationNetworkError, ValueError):
                        quota_conversation = None
                    retryable = bool(
                        envelope.type == "dm.group.message.committed"
                        or (
                            envelope.type == "dm.message.create"
                            and quota_conversation is not None
                            and quota_conversation.authority_domain != settings.domain
                        )
                    )
            inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
            if retryable:
                if inbox is not None:
                    await session.delete(inbox)
                    peer.federation_inbox_events = max(0, peer.federation_inbox_events - 1)
                    global_ledger.federation_inbox_events = max(
                        0, global_ledger.federation_inbox_events - 1
                    )
                await session.commit()
                return InboxResult(
                    event_id=envelope.event_id,
                    status="retry",
                    code="KAED_FED_DM_STORAGE_QUOTA_EXCEEDED",
                )
            if inbox is None:
                raise RuntimeError("federation inbox claim disappeared") from exc
            inbox.status = "rejected"
            inbox.result_code = "KAED_FED_DM_STORAGE_QUOTA_EXCEEDED"
            inbox.error = "federated DM capacity was reached"
            inbox.processed_at = datetime.now(UTC)
            # A queued open needs an explicit rejection event so the initiating
            # client can resolve its operation instead of waiting forever.
            if envelope.type == "dm.open.request":
                try:
                    profiles = DMOpenFederationRequest.model_validate(
                        {"participants": envelope.content.get("participants")}
                    ).participants
                    quota_dm_local_profile = next(
                        profile for profile in profiles if profile.origin_domain == settings.domain
                    )
                    quota_dm_local_user = await session.get(
                        User,
                        (
                            int(quota_dm_local_profile.id),
                            quota_dm_local_profile.origin_domain,
                        ),
                    )
                    if quota_dm_local_user is not None:
                        rejected = await build_envelope(
                            session,
                            settings,
                            "dm.open.rejected",
                            quota_dm_local_user,
                            {
                                "target": {
                                    "id": envelope.actor.id,
                                    "domain": envelope.actor.domain,
                                },
                                "pair_key": str(envelope.content.get("pair_key", "")),
                                "code": "KAED_FED_DM_STORAGE_QUOTA_EXCEEDED",
                            },
                        )
                        await queue_event(session, settings, envelope.origin, rejected)
                        delivery_wakes.add(envelope.origin)
                except (StopIteration, ValidationError, ValueError):
                    pass
            await session.commit()
            for destination in delivery_wakes:
                await enqueue_best_effort(federation_deliver, destination)
            return InboxResult(
                event_id=envelope.event_id,
                status="rejected",
                code="KAED_FED_DM_STORAGE_QUOTA_EXCEEDED",
            )
        if isinstance(exc, RelationshipQuotaExceeded):
            inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
            if inbox is None:
                raise RuntimeError("federation inbox claim disappeared") from exc
            inbox.status = "rejected"
            inbox.result_code = exc.code
            inbox.error = "pending relationship request capacity was reached"
            inbox.processed_at = datetime.now(UTC)
            await session.commit()
            return InboxResult(
                event_id=envelope.event_id,
                status="rejected",
                code=exc.code,
            )
        if isinstance(exc, (FederationIdentityQuotaExceeded, FederationInstanceQuotaExceeded)):
            delivery_wakes.clear()
            federation_code = exc.federation_code
            local_code = exc.code

            # A replica must not skip an event that introduces an identity.
            # Roll it back, persist a visible pause, and let the sender replay
            # after the operator raises capacity or cached state is reclaimed.
            capacity_guild: Guild | None = None
            try:
                quota_guild_id = database_snowflake(envelope.context.get("guild_id"), "guild id")
                quota_guild_origin = normalize_domain(str(envelope.context.get("guild_domain", "")))
            except (FederationNetworkError, ValueError):
                quota_guild_id = None
                quota_guild_origin = ""
            if quota_guild_id is not None and quota_guild_origin != settings.domain:
                await mark_replica_capacity_paused(
                    session,
                    settings,
                    quota_guild_id,
                    quota_guild_origin,
                    error_code=local_code,
                    internal_error=str(exc),
                )
                capacity_guild = await session.get(
                    Guild, (quota_guild_id, quota_guild_origin), populate_existing=True
                )
            if capacity_guild is not None:
                inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
                if inbox is not None:
                    await session.delete(inbox)
                    peer.federation_inbox_events = max(0, peer.federation_inbox_events - 1)
                    global_ledger.federation_inbox_events = max(
                        0, global_ledger.federation_inbox_events - 1
                    )
                await session.commit()
                await publish_dispatch(
                    redis,
                    guild_topic(capacity_guild.origin_domain, capacity_guild.id),
                    "GUILD_UPDATE",
                    guild_payload(capacity_guild),
                )
                return InboxResult(
                    event_id=envelope.event_id,
                    status="retry",
                    code=federation_code,
                )

            local_dm_rejection: tuple[int, str, dict[str, object]] | None = None
            if envelope.type in {"dm.open.request", "dm.conversation.create"}:
                try:
                    profiles = DMOpenFederationRequest.model_validate(
                        {"participants": envelope.content.get("participants")}
                    ).participants
                    handles = [
                        f"{profile.username}@{profile.origin_domain}" for profile in profiles
                    ]
                    pair_key = dm_pair_key(*handles)
                    local_profile = next(
                        profile for profile in profiles if profile.origin_domain == settings.domain
                    )
                    capacity_local_user = await session.get(
                        User, (int(local_profile.id), local_profile.origin_domain)
                    )
                    if capacity_local_user is not None and (
                        envelope.type != "dm.open.request"
                        or str(envelope.content.get("pair_key", "")) == pair_key
                    ):
                        if envelope.type == "dm.open.request":
                            rejected = await build_envelope(
                                session,
                                settings,
                                "dm.open.rejected",
                                capacity_local_user,
                                {
                                    "target": {
                                        "id": envelope.actor.id,
                                        "domain": envelope.actor.domain,
                                    },
                                    "pair_key": pair_key,
                                    "code": federation_code,
                                },
                            )
                            await queue_event(session, settings, envelope.origin, rejected)
                            delivery_wakes.add(envelope.origin)
                        else:
                            raw_conversation = envelope.content.get("conversation")
                            if (
                                isinstance(raw_conversation, dict)
                                and str(raw_conversation.get("pair_key", "")) == pair_key
                            ):
                                local_dm_rejection = (
                                    capacity_local_user.id,
                                    capacity_local_user.origin_domain,
                                    {"pair_key": pair_key, "code": local_code},
                                )
                except (StopIteration, ValidationError, ValueError):
                    pass

            # A proxy write rejected while introducing its actor needs the same
            # explicit optimistic-message failure as other authoritative
            # rejections, but carries the stable capacity code.
            if envelope.type == "guild.proxy.message.create":
                try:
                    async with session.begin_nested():
                        guild = await home_guild(
                            session,
                            settings,
                            database_snowflake(envelope.context.get("guild_id"), "guild id"),
                        )
                        owner = await session.get(User, (guild.owner_id, guild.owner_domain))
                        if owner is not None and owner.is_local:
                            rejected = await build_envelope(
                                session,
                                settings,
                                "message.send_rejected",
                                owner,
                                {
                                    "target": {
                                        "id": envelope.actor.id,
                                        "domain": envelope.actor.domain,
                                    },
                                    "channel_id": str(envelope.content.get("channel_id", "")),
                                    "client_nonce": str(envelope.content.get("client_nonce", "")),
                                    "code": federation_code,
                                },
                                context={
                                    "guild_id": str(guild.id),
                                    "guild_domain": guild.origin_domain,
                                },
                            )
                            await queue_event(session, settings, envelope.origin, rejected)
                            delivery_wakes.add(envelope.origin)
                except Exception:
                    delivery_wakes.clear()

            inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
            if inbox is None:
                raise RuntimeError("federation inbox claim disappeared") from exc
            inbox.status = "rejected"
            inbox.result_code = federation_code
            inbox.error = "federated identity capacity was reached"
            inbox.processed_at = datetime.now(UTC)
            await session.commit()
            for destination in delivery_wakes:
                await enqueue_best_effort(federation_deliver, destination)
            if local_dm_rejection is not None:
                await publish_dispatch(
                    redis,
                    user_topic(local_dm_rejection[1], local_dm_rejection[0]),
                    "DM_OPEN_REJECTED",
                    local_dm_rejection[2],
                )
            return InboxResult(
                event_id=envelope.event_id,
                status="rejected",
                code=federation_code,
            )
        if isinstance(exc, FederationReplicaQuotaExceeded):
            quota_guild: Guild | None = None
            try:
                quota_guild_id = database_snowflake(envelope.context.get("guild_id"), "guild id")
                quota_guild_origin = normalize_domain(str(envelope.context.get("guild_domain", "")))
            except (FederationNetworkError, ValueError):
                quota_guild_id = None
                quota_guild_origin = ""
            if quota_guild_id is not None:
                await mark_replica_quota_paused(
                    session,
                    settings,
                    quota_guild_id,
                    quota_guild_origin,
                    exc,
                )
                quota_guild = await session.get(
                    Guild, (quota_guild_id, quota_guild_origin), populate_existing=True
                )
            inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
            if inbox is not None:
                await session.delete(inbox)
                peer.federation_inbox_events = max(0, peer.federation_inbox_events - 1)
                global_ledger.federation_inbox_events = max(
                    0, global_ledger.federation_inbox_events - 1
                )
            await session.commit()
            if quota_guild is not None:
                await publish_dispatch(
                    redis,
                    guild_topic(quota_guild.origin_domain, quota_guild.id),
                    "GUILD_UPDATE",
                    guild_payload(quota_guild),
                )
            return InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code=REPLICA_QUOTA_ERROR_CODE,
            )
        if isinstance(exc, FederationResyncRetry):
            # A retry is nonterminal. Removing the still-uncommitted claim lets
            # the sender reapply the event after the replica has synchronized.
            resync_target: tuple[str, int] | None = None
            try:
                resync_guild_id = database_snowflake(envelope.context.get("guild_id"), "guild id")
                resync_origin = normalize_domain(str(envelope.context.get("guild_domain", "")))
                resync_sequence = database_snowflake(
                    envelope.context.get("seq") or envelope.context.get("snapshot_seq"),
                    "required guild sequence",
                )
            except (FederationNetworkError, ValueError):
                pass
            else:
                await mark_guild_replica_stale(
                    session,
                    settings,
                    resync_guild_id,
                    resync_origin,
                    resync_sequence,
                )
                resync_target = (resync_origin, resync_guild_id)
            inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
            if inbox is not None:
                await session.delete(inbox)
                peer.federation_inbox_events = max(0, peer.federation_inbox_events - 1)
                global_ledger.federation_inbox_events = max(
                    0, global_ledger.federation_inbox_events - 1
                )
            await session.commit()
            if resync_target is not None:
                await enqueue_best_effort(
                    federation_guild_sync,
                    resync_target[0],
                    resync_target[1],
                )
            return InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code="KAED_FED_RESYNC_RETRY",
            )
        delivery_wakes.clear()
        if envelope.type == "guild.proxy.message.create":
            try:
                async with session.begin_nested():
                    guild = await home_guild(
                        session,
                        settings,
                        database_snowflake(envelope.context.get("guild_id"), "guild id"),
                    )
                    owner = await session.get(User, (guild.owner_id, guild.owner_domain))
                    if owner is not None and owner.origin_domain == settings.domain:
                        rejection_detail = exc.detail if isinstance(exc, HTTPException) else None
                        code = (
                            str(rejection_detail.get("code"))
                            if isinstance(rejection_detail, dict) and rejection_detail.get("code")
                            else "FEDERATED_WRITE_REJECTED"
                        )
                        timeout_context: dict[str, object] = (
                            {
                                "reason": rejection_detail.get("reason"),
                                "timeout_until": rejection_detail.get("timeout_until"),
                                "timeout_indefinite": rejection_detail.get(
                                    "timeout_indefinite", False
                                ),
                            }
                            if code == "MEMBER_TIMED_OUT" and isinstance(rejection_detail, dict)
                            else {}
                        )
                        rejected = await build_envelope(
                            session,
                            settings,
                            "message.send_rejected",
                            owner,
                            {
                                "target": {
                                    "id": envelope.actor.id,
                                    "domain": envelope.actor.domain,
                                },
                                "channel_id": str(envelope.content.get("channel_id", "")),
                                "client_nonce": str(envelope.content.get("client_nonce", "")),
                                "code": code,
                                **timeout_context,
                            },
                            context={
                                "guild_id": str(guild.id),
                                "guild_domain": guild.origin_domain,
                            },
                        )
                        await queue_event(session, settings, envelope.origin, rejected)
                        delivery_wakes.add(envelope.origin)
            except Exception:
                delivery_wakes.clear()
        inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
        if inbox is None:
            raise RuntimeError("federation inbox claim disappeared") from exc
        inbox.status = "rejected"
        inbox.result_code = "KAED_FED_EVENT_REJECTED"
        inbox.error = str(exc)[:500]
        inbox.processed_at = datetime.now(UTC)
        await session.commit()
        for destination in delivery_wakes:
            await enqueue_best_effort(federation_deliver, destination)
        return InboxResult(
            event_id=envelope.event_id, status="rejected", code="KAED_FED_EVENT_REJECTED"
        )


@router.post("/_kaede/v1/inbox")
async def federation_inbox(
    request: Request,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    raw = await request.body()
    if len(raw) > 1024 * 1024:
        raise HTTPException(status_code=413, detail={"code": "KAED_FED_BATCH_TOO_LARGE"})
    payload = getattr(request.state, "federation_json", None)
    if payload is None:
        try:
            payload = strict_json_loads(raw)
        except ValueError:
            raise HTTPException(
                status_code=400, detail={"code": "KAED_FED_INVALID_BATCH"}
            ) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_INVALID_BATCH"})
    raw_events = payload["events"]
    if not 1 <= len(raw_events) <= 100:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_INVALID_BATCH_SIZE"})
    await enforce_origin_event_rate_limit(redis, principal.origin, len(raw_events))
    results: list[dict[str, object]] = []
    for raw_event in raw_events:
        try:
            event = EventEnvelope.model_validate(raw_event)
        except ValidationError:
            event_id = (
                str(raw_event.get("event_id", ""))[:64] if isinstance(raw_event, dict) else ""
            )
            results.append(
                InboxResult(
                    event_id=event_id,
                    status="rejected",
                    code="KAED_FED_INVALID_EVENT",
                ).model_dump()
            )
            continue
        policy_code = await federation_event_policy_code(
            session,
            principal.origin,
            event.type,
            deletion_control=(
                event.type == "media.delete"
                or terminal_room_event_ref(event.model_dump(mode="json")) is not None
                or guild_media_delete_request_ref(event.model_dump(mode="json")) is not None
            ),
        )
        if policy_code is not None:
            results.append(
                InboxResult(
                    event_id=event.event_id,
                    status="retry",
                    code=policy_code,
                ).model_dump()
            )
            continue
        result = await process_event(session, redis, settings, principal, event, snowflake)
        results.append(result.model_dump())
    return {"results": results}


@router.get("/_kaede/v1/users/lookup")
async def federation_user_lookup(
    handle: str = Query(max_length=286),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if principal.silenced:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    username, separator, domain = handle.lower().rpartition("@")
    if not separator or domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    user = await session.scalar(
        select(User).where(
            User.origin_domain == settings.domain,
            User.is_local.is_(True),
            User.username == username,
        )
    )
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    return profile_from_user(user)


@router.get("/_kaede/v1/users/profile")
async def federation_user_profile_by_ref(
    user_id: Snowflake,
    user_domain: str = Query(min_length=1, max_length=253),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return a home-signed public profile proof for an exact composite ID."""

    if principal.silenced:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    try:
        requested_domain = normalize_domain(user_domain)
    except FederationNetworkError:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"}) from None
    if requested_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "profile-by-ref",
        capacity=120,
        refill_per_minute=120,
    )
    user = await session.get(User, (int(user_id), settings.domain))
    if user is None or not user.is_local:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    return await build_envelope(
        session,
        settings,
        "user.profile",
        user,
        {
            "subject": {"id": str(user.id), "origin_domain": user.origin_domain},
            "profile": profile_from_user(user),
        },
    )


@router.post("/_kaede/v1/e2ee/key-packages/claim")
async def federation_e2ee_key_packages_claim(
    payload: E2EEKeyPackageClaimRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "e2ee-key-package-claim",
        capacity=1_000,
        refill_per_minute=1_000,
    )
    if (
        payload.channel_domain != principal.origin
        or payload.operation_domain != principal.origin
        or payload.target_domain != settings.domain
    ):
        raise HTTPException(status_code=403, detail={"code": "KAED_E2EE_AUTHORITY_MISMATCH"})
    channel = await session.get(
        Channel,
        (int(payload.channel_id), payload.channel_domain),
    )
    target = await session.get(User, (int(payload.target_id), payload.target_domain))
    if channel is None or target is None or not target.is_local:
        raise HTTPException(status_code=404, detail={"code": "KAED_E2EE_TARGET_NOT_FOUND"})
    claimant_ref = (int(payload.claimant_id), payload.claimant_domain)
    target_ref = (target.id, target.origin_domain)
    if channel.guild_id is not None:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        claimant = await session.get(User, claimant_ref)
        if guild is None or claimant is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "KAED_E2EE_TARGET_NOT_FOUND"},
            )
        await require_guild_key_package_claim_visibility(
            session,
            redis,
            guild,
            channel,
            claimant,
            target,
        )
        authorized = True
    else:
        target_participant = await session.get(
            DMParticipant,
            (channel.id, channel.origin_domain, target.id, target.origin_domain),
        )
        claimant_participant = await session.get(
            DMParticipant,
            (channel.id, channel.origin_domain, claimant_ref[0], claimant_ref[1]),
        )
        authorized = target_participant is not None and claimant_participant is not None
    if not authorized:
        raise HTTPException(status_code=404, detail={"code": "KAED_E2EE_TARGET_NOT_FOUND"})
    excluded = (payload.excluded_device_id or "") if claimant_ref == target_ref else ""
    packages = await claim_local_room_key_packages(
        session,
        [target],
        operation_id=payload.operation_id,
        operation_domain=payload.operation_domain,
        channel_ref=(channel.id, channel.origin_domain),
        claimant_ref=claimant_ref,
        excluded_device_id=excluded,
        max_devices=payload.max_devices,
    )
    await session.commit()
    return {"key_packages": packages}


async def require_guild_key_package_claim_visibility(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    channel: Channel,
    claimant: User,
    target: User,
) -> None:
    """Require both sides of a guild package claim to see the exact channel."""

    try:
        await require_permissions(
            session,
            redis,
            guild,
            claimant,
            Permission.VIEW_CHANNEL,
            channel=channel,
        )
        await require_permissions(
            session,
            redis,
            guild,
            target,
            Permission.VIEW_CHANNEL,
            channel=channel,
        )
    except HTTPException:
        # Package availability is sensitive membership and channel-visibility
        # metadata. Keep every authorization miss indistinguishable from a
        # missing target, including malformed synchronized-parent state.
        raise HTTPException(
            status_code=404,
            detail={"code": "KAED_E2EE_TARGET_NOT_FOUND"},
        ) from None


async def federated_e2ee_actor(
    payload: E2EERoomProxyRequest | E2EERoomOperationStatusRequest,
    principal: FederationPrincipal,
    session: AsyncSession,
    settings: Settings,
) -> AuthenticatedUser:
    if payload.actor.origin_domain != principal.origin or payload.channel_domain != settings.domain:
        raise HTTPException(status_code=403, detail={"code": "KAED_E2EE_AUTHORITY_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    return AuthenticatedUser(
        user=actor,
        grant=AccessGrant(actor.id, actor.origin_domain, f"federation:{principal.origin}"),
        access_token="",
        cookie_authenticated=False,
    )


async def enforce_e2ee_room_proxy_limit(redis: Redis, principal: FederationPrincipal) -> None:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "e2ee-room-proxy",
        capacity=120,
        refill_per_minute=120,
    )


@router.post("/_kaede/v1/e2ee/rooms/propose")
async def federation_e2ee_room_propose(
    payload: E2EERoomProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_e2ee_room_proxy_limit(redis, principal)
    auth = await federated_e2ee_actor(payload, principal, session, settings)
    return await propose_room_encryption(
        EntityRef(f"{payload.channel_id}@{payload.channel_domain}"),
        RoomProposalRequest(
            operation_id=payload.operation_id,
            sender_device_id=payload.sender_device_id,
        ),
        auth,
        session,
        redis,
        settings,
    )


@router.post("/_kaede/v1/e2ee/rooms/activate")
async def federation_e2ee_room_activate(
    payload: E2EERoomProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_e2ee_room_proxy_limit(redis, principal)
    auth = await federated_e2ee_actor(payload, principal, session, settings)
    if payload.vault_attested is not True:
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_ACCOUNT_VAULT_ATTESTATION_REQUIRED"},
        )
    activation = RoomActivationRequest.model_validate(
        {
            "operation_id": payload.operation_id,
            "sender_device_id": payload.sender_device_id,
            "policy_generation": payload.policy_generation,
            "epoch": payload.epoch,
            "group_id": payload.group_id,
            "commit": payload.commit,
            "welcome": payload.welcome,
            "prepared_vault_revision": payload.prepared_vault_revision,
            "prepared_vault_digest": payload.prepared_vault_digest,
        }
    )
    return await activate_room_encryption_attested(
        EntityRef(f"{payload.channel_id}@{payload.channel_domain}"),
        activation,
        auth,
        session,
        redis,
        snowflake,
        settings,
    )


@router.post("/_kaede/v1/e2ee/rooms/rekey/propose")
async def federation_e2ee_room_rekey_propose(
    payload: E2EERoomProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_e2ee_room_proxy_limit(redis, principal)
    auth = await federated_e2ee_actor(payload, principal, session, settings)
    return await propose_room_rekey(
        EntityRef(f"{payload.channel_id}@{payload.channel_domain}"),
        RoomProposalRequest(
            operation_id=payload.operation_id,
            sender_device_id=payload.sender_device_id,
        ),
        auth,
        session,
        redis,
        settings,
    )


@router.post("/_kaede/v1/e2ee/rooms/rekey/activate")
async def federation_e2ee_room_rekey_activate(
    payload: E2EERoomProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_e2ee_room_proxy_limit(redis, principal)
    auth = await federated_e2ee_actor(payload, principal, session, settings)
    if payload.vault_attested is not True:
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_ACCOUNT_VAULT_ATTESTATION_REQUIRED"},
        )
    activation = RoomRekeyActivationRequest.model_validate(
        {
            "operation_id": payload.operation_id,
            "sender_device_id": payload.sender_device_id,
            "policy_generation": payload.policy_generation,
            "epoch": payload.epoch,
            "group_id": payload.group_id,
            "commit": payload.commit,
            "welcome": payload.welcome,
            "prepared_vault_revision": payload.prepared_vault_revision,
            "prepared_vault_digest": payload.prepared_vault_digest,
        }
    )
    return await activate_room_rekey_attested(
        EntityRef(f"{payload.channel_id}@{payload.channel_domain}"),
        activation,
        auth,
        session,
        redis,
        snowflake,
        settings,
    )


@router.post("/_kaede/v1/e2ee/rooms/operations/status")
async def federation_e2ee_room_operation_status(
    payload: E2EERoomOperationStatusRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_e2ee_room_proxy_limit(redis, principal)
    auth = await federated_e2ee_actor(payload, principal, session, settings)
    return await room_encryption_operation_status_for_actor(
        EntityRef(f"{payload.channel_id}@{payload.channel_domain}"),
        payload.operation_id,
        auth,
        session,
        redis,
        settings,
    )


@router.post("/_kaede/v1/presence", status_code=204)
async def federation_presence_update(
    payload: PresenceFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Accept a signed, expiring presence projection from the user's home."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "presence", capacity=300, refill_per_minute=300
    )
    if payload.user_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    if not await receive_presence(session, redis, settings, payload):
        raise HTTPException(status_code=409, detail={"code": "KAED_PRESENCE_STALE_OR_UNKNOWN"})
    return Response(status_code=204)


def _strip_terminal_attachments(
    messages: Sequence[object],
    terminal_refs: set[tuple[int, str]],
) -> None:
    """Remove terminal media metadata while its ref fences are held."""

    if not terminal_refs:
        return
    for item in messages:
        if not isinstance(item, dict):
            continue
        attachments = item.get("attachments")
        if not isinstance(attachments, list):
            continue
        item["attachments"] = [
            raw
            for raw in attachments
            if not (
                isinstance(raw, dict)
                and bool(attachment_refs_from_payloads([{"attachments": [raw]}]) & terminal_refs)
            )
        ]


async def _redact_terminal_guild_events(
    session: AsyncSession,
    settings: Settings,
    guild_id: int,
    events: Sequence[object],
    terminal_refs: set[tuple[int, str]],
) -> list[dict[str, object]]:
    """Replace immutable signed events that disclose terminal media by seq."""

    guild = await session.get(Guild, (guild_id, settings.domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    rendered: list[dict[str, object]] = []
    owner: User | None = None
    for raw in events:
        if not isinstance(raw, dict):
            continue
        if not (message_attachment_refs(raw) & terminal_refs):
            rendered.append(raw)
            continue
        context = raw.get("context")
        if not isinstance(context, dict) or context.get("seq") is None:
            raise RuntimeError("terminal guild history event has no sequence")
        if owner is None:
            owner = await session.get(User, (guild.owner_id, guild.owner_domain))
        if owner is None or owner.origin_domain != settings.domain:
            raise RuntimeError("guild owner cannot sign a terminal-media redaction")
        replacement = await build_envelope(
            session,
            settings,
            "guild.event.redacted",
            owner,
            {"original_type": str(raw.get("type", ""))},
            context={
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "seq": str(context["seq"]),
                **(
                    {"snapshot_generation": str(context["snapshot_generation"])}
                    if context.get("snapshot_generation") is not None
                    else {}
                ),
                **({"snapshot_required": True} if guild_event_requires_snapshot(raw) else {}),
            },
        )
        rendered.append(cast(dict[str, object], replacement))
    return rendered


@router.get("/_kaede/v1/dms/{conversation_id}/messages")
async def federation_dm_history_page(
    conversation_id: Snowflake,
    before_id: int | None = Query(default=None, ge=0, le=MAX_SNOWFLAKE),
    before_domain: str | None = Query(default=None, min_length=1, max_length=253),
    limit: int = Query(default=50, ge=1, le=100),
    requester_id: int | None = Query(default=None, ge=0, le=MAX_SNOWFLAKE),
    requester_domain: str | None = Query(default=None, min_length=1, max_length=253),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return an authorized, bounded DM page without creating another replica."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "dm-history-page",
        capacity=3_000,
        refill_per_minute=3_000,
    )
    if (before_id is None) != (before_domain is None):
        raise HTTPException(status_code=400, detail={"code": "INVALID_PAGINATION"})
    conversation = await session.get(DMConversation, (int(conversation_id), settings.domain))
    if conversation is None or conversation.authority_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_DM_HISTORY_NOT_FOUND"})
    if (requester_id is None) != (requester_domain is None):
        raise HTTPException(status_code=400, detail={"code": "INVALID_REQUESTER"})
    if conversation.type == "group" and requester_id is None:
        raise HTTPException(status_code=400, detail={"code": "INVALID_REQUESTER"})
    if requester_domain is not None:
        try:
            requester_domain = normalize_domain(requester_domain)
        except FederationNetworkError:
            raise HTTPException(status_code=400, detail={"code": "INVALID_REQUESTER"}) from None
        if requester_domain != principal.origin:
            raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    participates = await session.scalar(
        select(DMParticipant.user_id)
        .where(
            DMParticipant.conversation_id == conversation.id,
            DMParticipant.conversation_domain == conversation.origin_domain,
            DMParticipant.user_domain == (requester_domain or principal.origin),
            *([DMParticipant.user_id == requester_id] if requester_id is not None else []),
        )
        .limit(1)
    )
    if participates is None:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_DM_HISTORY_FORBIDDEN"})
    conditions = [
        Message.channel_id == conversation.id,
        Message.channel_domain == conversation.origin_domain,
        ~exists(
            select(E2EEControlRecord.id).where(
                E2EEControlRecord.id == Message.id,
                E2EEControlRecord.origin_domain == Message.origin_domain,
            )
        ),
        # A peer may retrieve only bodies authored away from that peer. Its
        # locally-authored rows are durable source data on that home and are
        # merged from its own database; the authority is not trusted to echo
        # or rewrite them.
        Message.origin_domain != principal.origin,
    ]
    if before_id is not None and before_domain is not None:
        try:
            normalized_before_domain = normalize_domain(before_domain)
        except FederationNetworkError:
            raise HTTPException(status_code=400, detail={"code": "INVALID_PAGINATION"}) from None
        conditions.append(
            tuple_(Message.id, Message.origin_domain) < (before_id, normalized_before_domain)
        )
    messages = list(
        await session.scalars(
            select(Message)
            .where(*conditions)
            .order_by(Message.id.desc(), Message.origin_domain.desc())
            .limit(limit + 1)
        )
    )
    selected = messages[:limit]
    refs = {(message.id, message.origin_domain) for message in selected}
    author_refs = {(message.author_id, message.author_domain) for message in selected}
    authors = {
        (author.id, author.origin_domain): author
        for author in await session.scalars(
            select(User).where(tuple_(User.id, User.origin_domain).in_(author_refs))
        )
    }
    attachments: dict[tuple[int, str], list[Attachment]] = {}
    if refs:
        for attachment in await session.scalars(
            select(Attachment)
            .where(
                tuple_(Attachment.message_id, Attachment.message_domain).in_(refs),
                Attachment.deleted_at.is_(None),
            )
            .order_by(Attachment.id)
        ):
            attachments.setdefault(
                (int(attachment.message_id or 0), str(attachment.message_domain)), []
            ).append(attachment)
    rendered: list[dict[str, object]] = []
    for message in selected:
        author = authors.get((message.author_id, message.author_domain))
        if author is None:
            raise RuntimeError("DM history message author disappeared")
        item = message_payload(
            message,
            author,
            attachments.get((message.id, message.origin_domain), []),
        )
        probe = {
            "conversation_id": str(conversation.id),
            "conversation_domain": conversation.origin_domain,
            "messages": [*rendered, item],
            "next_before": {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
            },
            "complete": False,
        }
        if len(canonical_json(probe)) > MAX_DM_HISTORY_RESPONSE_BYTES:
            if not rendered:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "KAED_FED_HISTORY_MESSAGE_TOO_LARGE"},
                )
            break
        rendered.append(item)
    has_more = len(rendered) < len(messages)
    next_before = (
        {
            "id": str(rendered[-1]["id"]),
            "origin_domain": str(rendered[-1]["origin_domain"]),
        }
        if rendered and has_more
        else None
    )
    result: dict[str, object] = {
        "conversation_id": str(conversation.id),
        "conversation_domain": conversation.origin_domain,
        "messages": rendered,
        "next_before": next_before,
        "complete": not has_more,
    }
    if len(canonical_json(result)) > MAX_DM_HISTORY_RESPONSE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "KAED_FED_HISTORY_MESSAGE_TOO_LARGE"},
        )
    disclosed, terminal_wakes, terminal_refs = await record_disclosed_attachment_recipients(
        session,
        settings,
        attachment_refs_from_payloads(rendered),
        principal.origin,
        room_ref=(
            ("group_dm", conversation.id, conversation.origin_domain)
            if conversation.type == "group"
            else None
        ),
    )
    _strip_terminal_attachments(rendered, terminal_refs)
    if conversation.type == "group" or disclosed or terminal_wakes:
        await session.commit()
    for destination in terminal_wakes:
        await enqueue_best_effort(federation_deliver, destination)
    return result


async def _federation_media_attachment(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: FederationPrincipal,
    attachment_id: int,
    variant: str,
    *,
    expected_conversation: tuple[int, str] | None = None,
    expected_message: tuple[int, str] | None = None,
    requester: tuple[int, str] | None = None,
) -> Attachment:
    """Authorize media and optionally bind it to an exact DM history assertion."""

    if variant not in {"original", "thumbnail_128", "thumbnail_512", "thumbnail_1024", "poster"}:
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    attachment = await session.get(Attachment, (attachment_id, settings.domain))
    if (
        attachment is None
        or await session.get(
            MediaTombstoneSource,
            (attachment_id, settings.domain),
        )
        is not None
        or attachment.scan_status not in {"clean", "encrypted"}
        or attachment.deleted_at is not None
        or attachment.message_id is None
        or attachment.message_domain is None
    ):
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    if attachment.encryption_mode == "e2ee" and variant != "original":
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    message = await session.get(Message, (attachment.message_id, attachment.message_domain))
    channel = (
        await session.get(Channel, (message.channel_id, message.channel_domain))
        if message is not None
        else None
    )
    if message is None or message.deleted_at is not None or channel is None:
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    conversation = (
        await session.get(DMConversation, (channel.id, channel.origin_domain))
        if channel.guild_id is None
        else None
    )
    if conversation is not None and conversation.type == "group" and requester is None:
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    if (expected_conversation is not None or expected_message is not None) and (
        expected_conversation is None
        or expected_message is None
        or (attachment.message_id, attachment.message_domain) != expected_message
        or (message.channel_id, message.channel_domain) != expected_conversation
        or channel.guild_id is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    authorized = False
    if channel.guild_id is None:
        authorized = (
            await session.scalar(
                select(DMParticipant.user_id)
                .where(
                    DMParticipant.conversation_id == channel.id,
                    DMParticipant.conversation_domain == channel.origin_domain,
                    DMParticipant.user_domain
                    == (requester[1] if requester is not None else principal.origin),
                    *([DMParticipant.user_id == requester[0]] if requester is not None else []),
                )
                .limit(1)
            )
            is not None
        )
    else:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is not None:
            visible_channels = await cached_visible_guild_channels_for_origin(
                session,
                redis,
                guild,
                principal.origin,
            )
            authorized = any(
                (visible.id, visible.origin_domain) == (channel.id, channel.origin_domain)
                for visible in visible_channels
            )
    if not authorized:
        # Do not disclose whether the attachment exists to a non-participating peer.
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    await record_attachment_recipients(
        session,
        {(attachment.id, attachment.origin_domain)},
        principal.origin,
        room_ref=(
            ("guild", channel.guild_id, channel.guild_domain)
            if channel.guild_id is not None and channel.guild_domain is not None
            else (
                ("group_dm", channel.id, channel.origin_domain)
                if conversation is not None and conversation.type == "group"
                else None
            )
        ),
    )
    # The recipient ledger must be durable before authorization or bytes are
    # returned. A later membership/permission revocation cannot erase the only
    # evidence that this peer may retain a cache.
    await session.commit()
    refreshed_attachment = await session.scalar(
        select(Attachment)
        .where(
            Attachment.id == attachment.id,
            Attachment.origin_domain == attachment.origin_domain,
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        refreshed_attachment is None
        or await session.get(
            MediaTombstoneSource,
            (attachment_id, settings.domain),
            populate_existing=True,
        )
        is not None
        or refreshed_attachment.scan_status not in {"clean", "encrypted"}
        or refreshed_attachment.deleted_at is not None
        or refreshed_attachment.message_id is None
        or refreshed_attachment.message_domain is None
    ):
        # The ledger insert can wait behind the terminal worker's row lock.
        # Recheck after that transaction commits so rejected bytes are never
        # opened merely because they were clean during the first read.
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    refreshed_message = await session.scalar(
        select(Message)
        .where(
            Message.id == refreshed_attachment.message_id,
            Message.origin_domain == refreshed_attachment.message_domain,
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if refreshed_message is None or refreshed_message.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    refreshed_channel = await session.get(
        Channel,
        (refreshed_message.channel_id, refreshed_message.channel_domain),
        populate_existing=True,
    )
    if refreshed_channel is None:
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    if (expected_conversation is not None or expected_message is not None) and (
        expected_conversation is None
        or expected_message is None
        or (refreshed_attachment.message_id, refreshed_attachment.message_domain)
        != expected_message
        or (refreshed_message.channel_id, refreshed_message.channel_domain) != expected_conversation
        or refreshed_channel.guild_id is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    if refreshed_channel.guild_id is None:
        reauthorized = (
            await session.scalar(
                select(DMParticipant.user_id)
                .where(
                    DMParticipant.conversation_id == refreshed_channel.id,
                    DMParticipant.conversation_domain == refreshed_channel.origin_domain,
                    DMParticipant.user_domain
                    == (requester[1] if requester is not None else principal.origin),
                    *([DMParticipant.user_id == requester[0]] if requester is not None else []),
                )
                .limit(1)
            )
            is not None
        )
    else:
        refreshed_guild = await session.get(
            Guild,
            (refreshed_channel.guild_id, refreshed_channel.guild_domain),
            populate_existing=True,
        )
        reauthorized = False
        if refreshed_guild is not None:
            visible_channels = await cached_visible_guild_channels_for_origin(
                session,
                redis,
                refreshed_guild,
                principal.origin,
            )
            reauthorized = any(
                (visible.id, visible.origin_domain)
                == (refreshed_channel.id, refreshed_channel.origin_domain)
                for visible in visible_channels
            )
    if not reauthorized:
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    return refreshed_attachment


def _dm_history_media_scope(
    *,
    conversation_id: int | None,
    conversation_domain: str | None,
    message_id: int | None,
    message_domain: str | None,
) -> tuple[tuple[int, str], tuple[int, str]] | None:
    values = (conversation_id, conversation_domain, message_id, message_domain)
    if all(value is None for value in values):
        return None
    if (
        conversation_id is None
        or conversation_domain is None
        or message_id is None
        or message_domain is None
    ):
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    try:
        return (
            (int(conversation_id), normalize_domain(str(conversation_domain))),
            (int(message_id), normalize_domain(str(message_domain))),
        )
    except (FederationNetworkError, TypeError, ValueError):
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"}) from None


@router.get("/_kaede/v1/media/{attachment_id}/{variant}/authorize", status_code=204)
async def federation_dm_history_media_authorize(
    attachment_id: Snowflake,
    variant: str,
    conversation_id: int | None = Query(default=None, ge=0, le=MAX_SNOWFLAKE),
    conversation_domain: str | None = Query(default=None, min_length=1, max_length=253),
    message_id: int | None = Query(default=None, ge=0, le=MAX_SNOWFLAKE),
    message_domain: str | None = Query(default=None, min_length=1, max_length=253),
    requester_id: int | None = Query(default=None, ge=0, le=MAX_SNOWFLAKE),
    requester_domain: str | None = Query(default=None, min_length=1, max_length=253),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Confirm that a history attachment belongs to the exact asserted DM message."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "dm-history-media-authorize",
        capacity=3_000,
        refill_per_minute=3_000,
    )
    scope = _dm_history_media_scope(
        conversation_id=conversation_id,
        conversation_domain=conversation_domain,
        message_id=message_id,
        message_domain=message_domain,
    )
    if scope is None:
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    if (requester_id is None) != (requester_domain is None):
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    requester = None
    if requester_id is not None and requester_domain is not None:
        try:
            normalized_requester_domain = normalize_domain(requester_domain)
        except FederationNetworkError:
            raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"}) from None
        if normalized_requester_domain != principal.origin:
            raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
        requester = (requester_id, normalized_requester_domain)
    attachment = await _federation_media_attachment(
        session,
        redis,
        settings,
        principal,
        int(attachment_id),
        variant,
        expected_conversation=scope[0],
        expected_message=scope[1],
        requester=requester,
    )
    response = Response(
        status_code=204,
        headers={"X-Kaede-Media-Encryption": attachment.encryption_mode},
    )
    await session.commit()
    return response


@router.get("/_kaede/v1/media/{attachment_id}/{variant}")
async def federation_media_get(
    attachment_id: Snowflake,
    variant: str,
    conversation_id: int | None = Query(default=None, ge=0, le=MAX_SNOWFLAKE),
    conversation_domain: str | None = Query(default=None, min_length=1, max_length=253),
    message_id: int | None = Query(default=None, ge=0, le=MAX_SNOWFLAKE),
    message_domain: str | None = Query(default=None, min_length=1, max_length=253),
    requester_id: int | None = Query(default=None, ge=0, le=MAX_SNOWFLAKE),
    requester_domain: str | None = Query(default=None, min_length=1, max_length=253),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Stream a fixed-template media variant to an authenticated participating peer."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "media-get",
        capacity=1_200,
        refill_per_minute=1_200,
    )
    scope = _dm_history_media_scope(
        conversation_id=conversation_id,
        conversation_domain=conversation_domain,
        message_id=message_id,
        message_domain=message_domain,
    )
    if (requester_id is None) != (requester_domain is None):
        raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
    requester = None
    if requester_id is not None and requester_domain is not None:
        try:
            normalized_requester_domain = normalize_domain(requester_domain)
        except FederationNetworkError:
            raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"}) from None
        if normalized_requester_domain != principal.origin:
            raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
        requester = (requester_id, normalized_requester_domain)
    attachment = await _federation_media_attachment(
        session,
        redis,
        settings,
        principal,
        int(attachment_id),
        variant,
        expected_conversation=scope[0] if scope is not None else None,
        expected_message=scope[1] if scope is not None else None,
        requester=requester,
    )
    bucket = settings.media_attachments_bucket
    key = attachment.object_key
    content_type = attachment.detected_content_type or attachment.content_type
    if variant != "original":
        raw = attachment.variants.get(variant)
        if not isinstance(raw, dict) or not isinstance(raw.get("object_key"), str):
            raise HTTPException(status_code=404, detail={"code": "KAED_MEDIA_NOT_FOUND"})
        bucket = settings.media_derived_bucket
        key = raw["object_key"]
        content_type = str(raw.get("content_type", "application/octet-stream"))
    try:
        body = await S3Storage(settings).get(
            bucket, key, max_bytes=settings.media_max_attachment_bytes
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail={"code": "KAED_MEDIA_UNAVAILABLE"}) from exc
    await session.commit()
    headers = {
        "Cache-Control": "private, max-age=86400, immutable",
        "X-Content-Type-Options": "nosniff",
        "Content-Length": str(len(body)),
    }
    return Response(
        content=body,
        media_type=content_type,
        headers=headers,
    )


@router.post("/_kaede/v1/dm/groups/authorize", status_code=204)
async def federation_group_dm_authorize(
    payload: DMGroupAuthorizeRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "dm-group-authorize", capacity=120, refill_per_minute=120
    )
    direct_inviter_request = payload.inviter.origin_domain == principal.origin
    authority_request = (
        payload.conversation_id is not None and payload.conversation_domain == principal.origin
    )
    if not direct_inviter_request and not authority_request:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    inviter = await upsert_remote_user(session, settings, payload.inviter)
    invitee = await upsert_remote_user(session, settings, payload.invitee)
    if invitee.origin_domain != settings.domain or not invitee.is_local:
        raise HTTPException(status_code=400, detail={"code": "KAED_GROUP_DM_INVITEE_NOT_LOCAL"})
    await require_group_invite_friend(session, inviter, invitee)
    await session.commit()
    return Response(status_code=204)


async def authorize_group_invitee_at_home(
    session: AsyncSession,
    settings: Settings,
    conversation: DMConversation,
    actor: User,
    target: User,
) -> None:
    """Require the invitee's home to confirm a cross-origin group invitation."""

    if target.origin_domain == settings.domain:
        await require_group_invite_friend(session, actor, target)
        return
    try:
        authorization = await signed_request(
            session,
            settings,
            "POST",
            target.origin_domain,
            "/_kaede/v1/dm/groups/authorize",
            payload={
                "conversation_id": str(conversation.id),
                "conversation_domain": conversation.origin_domain,
                "inviter": profile_from_user(actor),
                "invitee": profile_from_user(target),
            },
            request_timeout=8,
            max_response_bytes=16 * 1024,
        )
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "KAED_GROUP_DM_INVITEE_HOME_UNREACHABLE"},
        ) from exc
    if authorization.status_code != 204:
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_GROUP_DM_INVITE_NOT_FRIEND"},
        )


@router.post("/_kaede/v1/dm/groups/mutate")
async def federation_group_dm_mutate(
    payload: DMGroupMutationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "dm-group-mutate", capacity=180, refill_per_minute=180
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    if payload.conversation_domain != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "KAED_GROUP_DM_WRONG_AUTHORITY"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    target = (
        await upsert_remote_user(session, settings, payload.target)
        if payload.target is not None
        else None
    )
    if payload.action == "add" and target is not None:
        unlocked_conversation, _ = await load_authoritative_group(
            session,
            settings,
            int(payload.conversation_id),
            payload.conversation_domain,
        )
        await require_group_member(session, unlocked_conversation, actor)
        await authorize_group_invitee_at_home(
            session,
            settings,
            unlocked_conversation,
            actor,
            target,
        )
    conversation, channel = await load_authoritative_group(
        session,
        settings,
        int(payload.conversation_id),
        payload.conversation_domain,
        for_update=True,
    )
    previous_owner = (conversation.owner_id, conversation.owner_domain)
    before, participants, deleted = await apply_authoritative_group_mutation(
        session,
        redis,
        settings,
        conversation,
        channel,
        actor,
        action=payload.action,
        target=target,
        name=payload.name,
    )
    notice_result = await create_group_mutation_notice(
        session,
        settings,
        snowflake,
        conversation,
        channel,
        actor,
        action=payload.action,
        target=target,
        previous_owner=previous_owner,
        participants=participants,
    )
    notice_message = notice_result[0] if notice_result is not None else None
    notice_payload = notice_result[1] if notice_result is not None else None
    notice_ref = (
        (notice_message.id, notice_message.origin_domain) if notice_message is not None else None
    )
    content = group_conversation_content(
        conversation,
        channel,
        participants,
        deleted=deleted,
        notice=notice_payload,
    )
    local_media_purges: list[tuple[int, str]] = []
    terminal_state_destinations: set[str] = set()
    media_delivery_wakes: set[str] = set()
    if deleted:
        (
            local_media_purges,
            terminal_state_destinations,
            media_delivery_wakes,
        ) = await prepare_terminal_channel_media(session, settings, channel)
    # The authenticated actor's home requested this mutation, while this
    # instance is the conversation authority that signs the resulting state.
    # Preserve the semantic actor for notice and transition validation on
    # replicas without opening remote-actor signing to any other event type.
    destinations = (
        {user.origin_domain for user in [*before, *participants]} | terminal_state_destinations
    ) - {settings.domain}
    if deleted:
        if participants or notice_payload is not None:
            raise RuntimeError("terminal group state must not retain participants or a notice")
        await queue_terminal_room_deletion(
            session,
            settings,
            room_kind="group_dm",
            room_id=conversation.id,
            room_domain=conversation.origin_domain,
            actor=actor,
            event_type="dm.group.state",
            content=content,
            context={},
            destinations=destinations,
        )
    else:
        envelope = await build_envelope(
            session,
            settings,
            "dm.group.state",
            actor,
            content,
            authority_attested_actor=True,
        )
        for destination in destinations:
            await queue_event(session, settings, destination, envelope)
    before_refs = {(item.id, item.origin_domain) for item in before}
    conversation_ref = (conversation.id, conversation.origin_domain)
    await session.commit()
    conversation, channel, participants = await reload_group_projection(session, *conversation_ref)
    after_refs = {(item.id, item.origin_domain) for item in participants}
    for user in participants:
        if user.origin_domain != settings.domain or not user.is_local:
            continue
        await publish_dispatch(
            redis,
            user_topic(settings.domain, user.id),
            (
                "CHANNEL_CREATE"
                if (user.id, user.origin_domain) not in before_refs
                else "CHANNEL_UPDATE"
            ),
            dm_channel_payload(
                channel,
                [
                    item
                    for item in participants
                    if (item.id, item.origin_domain) != (user.id, user.origin_domain)
                ],
                conversation=conversation,
            ),
        )
    if notice_ref is not None:
        committed_notice = await session.get(Message, notice_ref, populate_existing=True)
        if committed_notice is None:
            raise RuntimeError("committed group DM notice disappeared")
        rendered_notice = await render_message_payload(session, committed_notice)
        for user in participants:
            if user.origin_domain == settings.domain and user.is_local:
                await publish_dispatch(
                    redis,
                    user_topic(settings.domain, user.id),
                    "MESSAGE_CREATE",
                    rendered_notice,
                )
    for user_id, user_domain in before_refs - after_refs:
        if user_domain == settings.domain:
            await publish_dispatch(
                redis,
                user_topic(settings.domain, user_id),
                "CHANNEL_DELETE",
                {"id": str(channel.id), "origin_domain": channel.origin_domain},
            )
    for destination in destinations | media_delivery_wakes:
        await enqueue_best_effort(federation_deliver, destination)
    for attachment_id, attachment_domain in local_media_purges:
        await enqueue_best_effort(media_local_purge, attachment_id, attachment_domain)
    return content


@router.post("/_kaede/v1/dm/open")
async def federation_dm_open(
    payload: DMOpenFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if principal.origin not in {item.origin_domain for item in payload.participants}:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    users = [await upsert_remote_user(session, settings, item) for item in payload.participants]
    local_recipient = next((user for user in users if user.origin_domain == settings.domain), None)
    remote_sender = next((user for user in users if user.origin_domain == principal.origin), None)
    if local_recipient is None or remote_sender is None:
        raise HTTPException(status_code=400, detail={"code": "KAED_DM_INVALID_PARTICIPANTS"})
    await require_can_direct_message(session, remote_sender, local_recipient)
    try:
        conversation, channel, users, created = await authoritative_dm_conversation(
            session, settings, snowflake, payload.participants
        )
    except FederatedDMQuotaExceeded as exc:
        raise HTTPException(status_code=507, detail=exc.detail(federation=True)) from exc
    await session.commit()
    participants = await session.scalars(
        select(User)
        .join(
            DMParticipant,
            (DMParticipant.user_id == User.id) & (DMParticipant.user_domain == User.origin_domain),
        )
        .where(
            DMParticipant.conversation_id == channel.id,
            DMParticipant.conversation_domain == channel.origin_domain,
        )
    )
    participant_list = list(participants)
    if created:
        await publish_dispatch(
            redis,
            user_topic(settings.domain, local_recipient.id),
            "CHANNEL_CREATE",
            dm_channel_payload(
                channel,
                [
                    user
                    for user in participant_list
                    if (user.id, user.origin_domain)
                    != (local_recipient.id, local_recipient.origin_domain)
                ],
                conversation=conversation,
            ),
        )
    return {
        "conversation": {
            "id": str(conversation.id),
            "origin_domain": conversation.origin_domain,
            "pair_key": conversation.pair_key,
            "authority_domain": conversation.authority_domain,
        },
        "channel": channel_payload(channel),
        "participants": [profile_from_user(user) for user in participant_list],
    }


@router.post("/_kaede/v1/dm/authorize")
async def federation_dm_authorize(
    payload: DMOpenFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    users = [await upsert_remote_user(session, settings, item) for item in payload.participants]
    local_recipient = next((user for user in users if user.origin_domain == settings.domain), None)
    remote_sender = next((user for user in users if user.origin_domain == principal.origin), None)
    if local_recipient is None or remote_sender is None:
        raise HTTPException(status_code=400, detail={"code": "KAED_DM_INVALID_PARTICIPANTS"})
    await require_can_direct_message(session, remote_sender, local_recipient)
    await session.commit()
    return {"status": "authorized"}


@router.post("/_kaede/v1/invites/resolve")
async def federation_invite_resolve(
    payload: InviteResolveRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "invite-resolve", capacity=30, refill_per_minute=30
    )
    if principal.silenced:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    invite = await session.get(Invite, payload.code)
    if invite is None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    if not active_invite(invite):
        # Permit only the instance that already consumed this invite to recover
        # a lost successful join response. This does not reopen the invite for
        # another user; /join binds replay to the existing composite member.
        prior_member = await session.scalar(
            select(GuildMember.user_id).where(
                GuildMember.guild_id == invite.guild_id,
                GuildMember.guild_domain == invite.guild_domain,
                GuildMember.user_domain == principal.origin,
            )
        )
        if prior_member is None:
            raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    guild = await home_guild(session, settings, invite.guild_id)
    return {
        "code": invite.code,
        "guild": guild_payload(guild),
        "channel_id": str(invite.channel_id) if invite.channel_id is not None else None,
    }


@router.post("/_kaede/v1/guilds/{guild_id}/join")
async def federation_guild_join(
    guild_id: Snowflake,
    payload: GuildJoinRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "guild-join", capacity=10, refill_per_minute=10
    )
    delivery_destinations: set[str] = set()
    e2ee_policy_channels: list[Channel] = []
    if payload.user.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    user = await upsert_remote_user(session, settings, payload.user)
    await require_remote_user_join_allowed(session, user)
    invite = await session.scalar(
        select(Invite).where(Invite.code == payload.code).with_for_update()
    )
    if invite is None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    if invite.guild_id != guild_id:
        # Do not let a valid invite code be replayed against a different guild
        # resource, and do not disclose which half of the pair was incorrect.
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    guild = await home_guild(session, settings, invite.guild_id)
    locked_guild = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked_guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    guild = locked_guild
    now = datetime.now(UTC)
    banned = await session.scalar(
        select(Ban).where(
            Ban.guild_id == guild.id,
            Ban.guild_domain == guild.origin_domain,
            Ban.user_id == user.id,
            Ban.user_domain == user.origin_domain,
            or_(Ban.expires_at.is_(None), Ban.expires_at > now),
        )
    )
    if banned is not None:
        raise HTTPException(status_code=403, detail={"code": "BANNED_FROM_GUILD"})
    instance_banned = await session.scalar(
        select(GuildInstanceBan.instance_domain).where(
            GuildInstanceBan.guild_id == guild.id,
            GuildInstanceBan.guild_domain == guild.origin_domain,
            GuildInstanceBan.instance_domain == principal.origin,
            or_(
                GuildInstanceBan.expires_at.is_(None),
                GuildInstanceBan.expires_at > now,
            ),
        )
    )
    if instance_banned is not None:
        raise HTTPException(status_code=403, detail={"code": "INSTANCE_BANNED_FROM_GUILD"})
    member = await session.get(
        GuildMember, (guild.id, guild.origin_domain, user.id, user.origin_domain)
    )
    await record_room_federation_recipient(
        session,
        ("guild", guild.id, guild.origin_domain),
        principal.origin,
    )
    if member is None:
        if not active_invite(invite):
            raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
        member = GuildMember(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=user.id,
            user_domain=user.origin_domain,
            joined_at=datetime.now(UTC),
        )
        session.add(member)
        if user.account_type != "bot":
            e2ee_policy_channels.extend(
                await pause_guild_e2ee_for_membership_change(session, guild)
            )
        invite.uses += 1
        guild.snapshot_generation += 1
        seq = await assign_guild_sequence(session, guild)
        owner = await session.get(User, (guild.owner_id, guild.owner_domain))
        if owner is None or owner.origin_domain != settings.domain:
            raise RuntimeError("guild owner cannot sign the membership event")
        member_event = await build_envelope(
            session,
            settings,
            "guild.member.add",
            owner,
            {
                "user": profile_from_user(user),
                "joined_at": member.joined_at.isoformat(),
            },
            context={
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "seq": str(seq),
                "snapshot_generation": str(guild.snapshot_generation),
            },
        )
        store_guild_event(
            session,
            guild,
            seq,
            str(member_event["event_id"]),
            member_event,
        )
        delivery_destinations = set(
            await session.scalars(
                select(GuildMember.user_domain)
                .where(
                    GuildMember.guild_id == guild.id,
                    GuildMember.guild_domain == guild.origin_domain,
                    GuildMember.user_domain.not_in((settings.domain, principal.origin)),
                )
                .distinct()
            )
        )
        for destination in delivery_destinations:
            await queue_event(session, settings, destination, member_event)
        await session.commit()
        await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_ADD",
            {"guild_id": str(guild.id), "user": user_payload(user)},
        )
        for destination in delivery_destinations:
            await enqueue_best_effort(federation_deliver, destination)
    else:
        seq = guild.next_event_seq - 1
        await session.commit()
    # assign_guild_sequence updates server-managed timestamps. SQLAlchemy
    # expires those attributes even when expire_on_commit is disabled, so an
    # explicit async refresh is required before the synchronous payload helper
    # reads the resource version.
    await session.refresh(guild)
    return {"guild": guild_payload(guild), "snapshot_seq": str(seq)}


@router.delete("/_kaede/v1/guilds/{guild_id}/members/@me", status_code=204)
async def federation_guild_leave(
    guild_id: Snowflake,
    payload: GuildLeaveRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "guild-leave", capacity=20, refill_per_minute=20
    )
    if payload.user.domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    guild = await session.scalar(
        select(Guild)
        .where(Guild.id == guild_id, Guild.origin_domain == settings.domain)
        .with_for_update()
    )
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    user_id = database_snowflake(payload.user.id, "guild leave user id")
    e2ee_policy_channels: list[Channel] = []
    _, deleted_role_refs, removed_thread_members = await _apply_authoritative_guild_leave(
        session,
        settings,
        guild,
        user_id=user_id,
        user_domain=payload.user.domain,
        missing_ok=False,
        e2ee_policy_channels=e2ee_policy_channels,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
    await publish_deleted_installation_roles(redis, guild, deleted_role_refs)
    await publish_guild_thread_member_cleanup(redis, guild, removed_thread_members)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_MEMBER_REMOVE",
        {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "user_id": str(user_id),
            "user_domain": payload.user.domain,
        },
    )
    return Response(status_code=204)


@router.get("/_kaede/v1/guilds/{guild_id}/members/{user_id}/moderation-status")
async def federation_self_moderation_status(
    guild_id: Snowflake,
    user_id: Snowflake,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return one member's private timeout details to their signed home only."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "member-self-moderation",
        capacity=1_200,
        refill_per_minute=1_200,
    )
    guild = await home_guild(session, settings, int(guild_id), for_share=True)
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, int(user_id), principal.origin),
    )
    if member is None:
        # Do not distinguish a missing account from a missing membership to a
        # peer. The signing origin may request only identities it hosts.
        raise HTTPException(status_code=404, detail={"code": "NOT_A_GUILD_MEMBER"})
    return guild_self_moderation_status(member).model_dump(mode="json")


async def require_origin_guild_member(session: AsyncSession, guild: Guild, origin: str) -> None:
    member = await session.scalar(
        select(GuildMember.user_id).where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_domain == origin,
        )
    )
    if member is None:
        raise HTTPException(status_code=403, detail={"code": "NOT_A_GUILD_MEMBER"})


async def visible_guild_channels_for_origin(
    session: AsyncSession,
    guild: Guild,
    origin: str,
    *,
    loaded_roles: list[Role] | None = None,
    loaded_channels: list[Channel] | None = None,
) -> list[Channel]:
    """Return origin-visible channels using a bounded, bulk-loaded permission graph."""

    channels = loaded_channels
    if channels is None:
        channel_rows = list(
            await session.scalars(
                select(Channel)
                .where(
                    Channel.guild_id == guild.id,
                    Channel.guild_domain == guild.origin_domain,
                    Channel.unavailable.is_(False),
                )
                .order_by(Channel.position, Channel.id)
                .limit(MAX_SNAPSHOT_VISIBILITY_CHANNELS + 1)
            )
        )
        if len(channel_rows) > MAX_SNAPSHOT_VISIBILITY_CHANNELS:
            raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})
        channels = channel_rows
    elif len(channels) > MAX_SNAPSHOT_VISIBILITY_CHANNELS:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})

    roles = loaded_roles
    if roles is None:
        role_rows = list(
            await session.scalars(
                select(Role)
                .where(Role.guild_id == guild.id, Role.guild_domain == guild.origin_domain)
                .limit(MAX_SNAPSHOT_VISIBILITY_ROLES + 1)
            )
        )
        if len(role_rows) > MAX_SNAPSHOT_VISIBILITY_ROLES:
            raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})
        roles = role_rows
    elif len(roles) > MAX_SNAPSHOT_VISIBILITY_ROLES:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})

    origin_members = list(
        await session.scalars(
            select(GuildMember)
            .where(
                GuildMember.guild_id == guild.id,
                GuildMember.guild_domain == guild.origin_domain,
                GuildMember.user_domain == origin,
            )
            .limit(MAX_SNAPSHOT_VISIBILITY_MEMBERS + 1)
        )
    )
    if len(origin_members) > MAX_SNAPSHOT_VISIBILITY_MEMBERS:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})
    if len(channels) * len(origin_members) > MAX_SNAPSHOT_VISIBILITY_CHECKS:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})

    member_role_rows = list(
        await session.scalars(
            select(MemberRole)
            .where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                MemberRole.user_domain == origin,
            )
            .limit(MAX_SNAPSHOT_VISIBILITY_MEMBER_ROLES + 1)
        )
    )
    if len(member_role_rows) > MAX_SNAPSHOT_VISIBILITY_MEMBER_ROLES:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})

    overwrite_rows = list(
        await session.scalars(
            select(ChannelOverwrite)
            .where(
                ChannelOverwrite.guild_id == guild.id,
                ChannelOverwrite.guild_domain == guild.origin_domain,
            )
            .limit(MAX_SNAPSHOT_VISIBILITY_OVERWRITES + 1)
        )
    )
    if len(overwrite_rows) > MAX_SNAPSHOT_VISIBILITY_OVERWRITES:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})

    roles_by_ref = {(role.id, role.origin_domain): role for role in roles}
    role_refs_by_member: dict[tuple[int, str], set[tuple[int, str]]] = {}
    for member_role in member_role_rows:
        role_refs_by_member.setdefault((member_role.user_id, member_role.user_domain), set()).add(
            (member_role.role_id, member_role.role_domain)
        )
    overwrites_by_channel: dict[tuple[int, str], list[PermissionOverwrite]] = {}
    for overwrite in overwrite_rows:
        overwrites_by_channel.setdefault(
            (overwrite.channel_id, overwrite.channel_domain), []
        ).append(
            PermissionOverwrite(
                overwrite.target_id,
                overwrite.target_domain,
                overwrite.target_type,
                overwrite.allow,
                overwrite.deny,
            )
        )
    channels_by_ref = {(channel.id, channel.origin_domain): channel for channel in channels}
    now = datetime.now(UTC)
    everyone_ref = (guild.id, guild.origin_domain)
    member_permission_inputs: list[
        tuple[GuildMember, tuple[int, str], set[tuple[int, str]], int]
    ] = []
    for member in origin_members:
        member_ref = (member.user_id, member.user_domain)
        role_refs = {everyone_ref, *role_refs_by_member.get(member_ref, set())}
        member_permission_inputs.append(
            (
                member,
                member_ref,
                role_refs,
                reduce(
                    bit_or,
                    (
                        roles_by_ref[role_ref].permissions
                        for role_ref in role_refs
                        if role_ref in roles_by_ref
                    ),
                    0,
                ),
            )
        )
    visibility_work = 0
    for channel in channels:
        permission_channel = channel
        if channel.parent_id is not None and channel.permissions_synced:
            parent = channels_by_ref.get((channel.parent_id, str(channel.parent_domain)))
            if parent is None or parent.type != 4:
                raise HTTPException(status_code=409, detail={"code": "CHANNEL_PARENT_INVALID"})
            permission_channel = parent
        visibility_work += len(origin_members) * (
            1
            + 3
            * len(
                overwrites_by_channel.get(
                    (permission_channel.id, permission_channel.origin_domain), []
                )
            )
        )
        if visibility_work > MAX_SNAPSHOT_VISIBILITY_CHECKS:
            raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})
    visible: list[Channel] = []
    for channel in channels:
        permission_channel = channel
        if channel.parent_id is not None and channel.permissions_synced:
            parent = channels_by_ref.get((channel.parent_id, str(channel.parent_domain)))
            if parent is None or parent.type != 4:
                raise HTTPException(status_code=409, detail={"code": "CHANNEL_PARENT_INVALID"})
            permission_channel = parent
        channel_overwrites = overwrites_by_channel.get(
            (permission_channel.id, permission_channel.origin_domain), []
        )
        for member, member_ref, role_refs, base_permissions in member_permission_inputs:
            permissions = resolve_permissions(
                owner=member_ref == (guild.owner_id, guild.owner_domain),
                user_id=member.user_id,
                user_domain=member.user_domain,
                everyone_role_id=guild.id,
                everyone_role_domain=guild.origin_domain,
                role_ids=role_refs,
                base_permissions=base_permissions,
                overwrites=channel_overwrites,
                channel_type=channel.type,
                timed_out=member.timeout_indefinite
                or (member.timeout_until is not None and member.timeout_until > now),
            )
            if permissions & Permission.VIEW_CHANNEL:
                visible.append(channel)
                break
    return visible


def guild_visibility_cache_key(guild: Guild, origin: str) -> str:
    return (
        f"federation:snapshot-visible:{origin}:{guild.origin_domain}:{guild.id}:"
        f"{guild.permission_generation}:{guild.snapshot_generation}"
    )


def _guild_snapshot_cursor_changed(
    *,
    current_seq: int,
    current_generation: int,
    requested_seq: int,
    requested_generation: int | None,
) -> bool:
    if requested_seq > current_seq:
        return True
    if requested_generation is None:
        # Legacy peers invalidate on any event, including an ordinary message.
        return requested_seq != current_seq
    return requested_generation != current_generation


async def cached_visible_guild_channels_for_origin(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    origin: str,
    *,
    loaded_roles: list[Role] | None = None,
) -> list[Channel]:
    cache_key = guild_visibility_cache_key(guild, origin)
    cached_channel_ids: list[int] | None = None
    cached_visibility = await redis.get(cache_key)
    if isinstance(cached_visibility, (str, bytes)):
        try:
            raw_ids = json.loads(cached_visibility)
            if (
                isinstance(raw_ids, list)
                and len(raw_ids) <= MAX_SNAPSHOT_VISIBILITY_CHANNELS
                and all(isinstance(item, int) and not isinstance(item, bool) for item in raw_ids)
            ):
                cached_channel_ids = raw_ids
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    if cached_channel_ids is None:
        channels = await visible_guild_channels_for_origin(
            session,
            guild,
            origin,
            loaded_roles=loaded_roles,
        )
        await redis.set(
            cache_key,
            json.dumps([channel.id for channel in channels], separators=(",", ":")),
            ex=300,
        )
        return channels
    if not cached_channel_ids:
        return []
    cached_channels = list(
        await session.scalars(
            select(Channel).where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                Channel.unavailable.is_(False),
                Channel.id.in_(cached_channel_ids),
            )
        )
    )
    by_id = {channel.id: channel for channel in cached_channels}
    return [by_id[channel_id] for channel_id in cached_channel_ids if channel_id in by_id]


@router.post("/_kaede/v1/guilds/{guild_id}/history-exports")
async def federation_history_export_create(
    guild_id: Snowflake,
    payload: GuildHistoryExportRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "guild-history-create", capacity=10, refill_per_minute=10
    )
    if payload.user.domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_HISTORY_FORBIDDEN"})
    guild = await home_guild(session, settings, guild_id, for_share=True)
    await record_room_federation_recipient(
        session,
        ("guild", guild.id, guild.origin_domain),
        principal.origin,
    )
    user = await session.get(User, (int(payload.user.id), payload.user.domain))
    if user is None:
        raise HTTPException(status_code=403, detail={"code": "NOT_A_GUILD_MEMBER"})
    export = await create_history_export(
        session,
        settings,
        snowflake,
        guild,
        user,
        principal.origin,
    )
    if export is None:
        await session.commit()
        return {"available": False}
    await session.commit()
    return {
        "available": True,
        **await history_export_manifest(session, export.id, principal.origin),
    }


@router.get("/_kaede/v1/guilds/{guild_id}/history-exports/{export_id}")
async def federation_history_export_manifest(
    guild_id: Snowflake,
    export_id: Snowflake,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "guild-history-manifest", capacity=60, refill_per_minute=60
    )
    manifest = await history_export_manifest(session, export_id, principal.origin)
    if database_snowflake(manifest["guild_id"], "history guild id") != guild_id:
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_HISTORY_NOT_FOUND"})
    await record_room_federation_recipient(
        session,
        ("guild", int(guild_id), settings.domain),
        principal.origin,
    )
    await session.commit()
    return manifest


@router.get("/_kaede/v1/guilds/{guild_id}/history-exports/{export_id}/channels/{channel_id}")
async def federation_history_export_channel(
    guild_id: Snowflake,
    export_id: Snowflake,
    channel_id: Snowflake,
    after: int = Query(default=0, ge=0, le=MAX_SNOWFLAKE),
    before: int | None = Query(default=None, ge=0, le=MAX_SNOWFLAKE),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "guild-history-page", capacity=300, refill_per_minute=300
    )
    manifest = await history_export_manifest(session, export_id, principal.origin)
    if database_snowflake(manifest["guild_id"], "history guild id") != guild_id:
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_HISTORY_NOT_FOUND"})
    page = await history_export_page(
        session,
        settings,
        export_id,
        principal.origin,
        channel_id,
        after,
        before=before,
    )
    if (
        page["channel_domain"] != settings.domain
        or database_snowflake(page["export_id"], "history export id") != export_id
    ):
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_HISTORY_NOT_FOUND"})
    _disclosed, terminal_wakes, terminal_refs = await record_disclosed_attachment_recipients(
        session,
        settings,
        attachment_refs_from_payloads(page.get("messages")),
        principal.origin,
        room_ref=("guild", int(guild_id), settings.domain),
    )
    messages = page.get("messages")
    if isinstance(messages, list):
        _strip_terminal_attachments(messages, terminal_refs)
    # The room-recipient ledger is durable access history even when this page
    # contains no attachments, so commit it before returning the response.
    await session.commit()
    for destination in terminal_wakes:
        await enqueue_best_effort(federation_deliver, destination)
    return page


@router.get("/_kaede/v1/guilds/{guild_id}/history-exports/{export_id}/delta")
async def federation_history_export_changes(
    guild_id: Snowflake,
    export_id: Snowflake,
    after_seq: int = Query(ge=0, le=MAX_SNOWFLAKE),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "guild-history-delta", capacity=120, refill_per_minute=120
    )
    manifest = await history_export_manifest(session, export_id, principal.origin)
    if database_snowflake(manifest["guild_id"], "history guild id") != guild_id:
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_HISTORY_NOT_FOUND"})
    delta = await history_export_delta(session, export_id, principal.origin, after_seq)
    delta_events = delta.get("events")
    attachment_refs: set[tuple[int, str]] = set()
    if isinstance(delta_events, list):
        for event in delta_events:
            if isinstance(event, dict):
                attachment_refs.update(message_attachment_refs(event))
    _disclosed, terminal_wakes, terminal_refs = await record_disclosed_attachment_recipients(
        session,
        settings,
        attachment_refs,
        principal.origin,
        room_ref=("guild", int(guild_id), settings.domain),
    )
    if isinstance(delta_events, list):
        delta["events"] = await _redact_terminal_guild_events(
            session,
            settings,
            int(guild_id),
            delta_events,
            terminal_refs,
        )
    await session.commit()
    for destination in terminal_wakes:
        await enqueue_best_effort(federation_deliver, destination)
    return delta


@router.post("/_kaede/v1/guilds/{guild_id}/history-exports/{export_id}/complete", status_code=204)
async def federation_history_export_complete(
    guild_id: Snowflake,
    export_id: Snowflake,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "guild-history-complete", capacity=30, refill_per_minute=30
    )
    manifest = await history_export_manifest(session, export_id, principal.origin)
    if database_snowflake(manifest["guild_id"], "history guild id") != guild_id:
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_HISTORY_NOT_FOUND"})
    await complete_history_export(session, export_id, principal.origin)
    await record_room_federation_recipient(
        session,
        ("guild", int(guild_id), settings.domain),
        principal.origin,
    )
    await session.commit()
    return Response(status_code=204)


@router.get("/_kaede/v1/guilds/{guild_id}/snapshot")
async def federation_guild_snapshot(
    guild_id: Snowflake,
    member_after_domain: str | None = Query(default=None, max_length=253),
    member_after_id: str | None = Query(default=None, max_length=19),
    member_snapshot_at: datetime | None = Query(default=None),
    member_snapshot_seq: str | None = Query(default=None, max_length=19),
    member_snapshot_generation: str | None = Query(default=None, max_length=19),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    cursor_values = (
        member_after_domain,
        member_after_id,
        member_snapshot_at,
        member_snapshot_seq,
        member_snapshot_generation,
    )
    cursor_present = any(value is not None for value in cursor_values)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        f"guild-snapshot-{'page' if cursor_present else 'start'}",
        capacity=300 if cursor_present else 10,
        refill_per_minute=300 if cursor_present else 10,
    )
    guild = await home_guild(session, settings, guild_id, for_share=True)
    await require_origin_guild_member(session, guild, principal.origin)
    admitted = await session.scalar(
        select(
            func.pg_try_advisory_xact_lock(
                func.hashtextextended(
                    f"kaede-guild-visibility:{principal.origin}:{guild.origin_domain}:{guild.id}",
                    0,
                )
            )
        )
    )
    if admitted is not True:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_BUSY"})
    member_cursor: tuple[str, int] | None = None
    current_snapshot_seq = guild.next_event_seq - 1
    current_snapshot_generation = guild.snapshot_generation
    snapshot_seq = current_snapshot_seq
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        guild_snapshot_rate_scope(
            guild.id,
            current_snapshot_generation,
            paginated=cursor_present,
        ),
        capacity=120 if cursor_present else 4,
        refill_per_minute=120 if cursor_present else 4,
    )
    if cursor_present:
        if (
            member_after_domain is None
            or member_after_id is None
            or member_snapshot_at is None
            or member_snapshot_seq is None
        ):
            raise HTTPException(
                status_code=400, detail={"code": "KAED_FED_INVALID_SNAPSHOT_CURSOR"}
            )
        try:
            member_cursor = (
                normalize_domain(member_after_domain),
                database_snowflake(member_after_id, "member cursor id"),
            )
            requested_snapshot_seq = database_snowflake(
                member_snapshot_seq, "member snapshot sequence"
            )
            requested_snapshot_generation = (
                database_snowflake(
                    member_snapshot_generation,
                    "member snapshot generation",
                )
                if member_snapshot_generation is not None
                else None
            )
            if requested_snapshot_generation is not None and requested_snapshot_generation < 1:
                raise ValueError("member snapshot generation must be positive")
        except (FederationNetworkError, ValueError):
            raise HTTPException(
                status_code=400, detail={"code": "KAED_FED_INVALID_SNAPSHOT_CURSOR"}
            ) from None
        if member_snapshot_at.tzinfo is None or member_snapshot_at > datetime.now(UTC) + timedelta(
            minutes=5
        ):
            raise HTTPException(
                status_code=400, detail={"code": "KAED_FED_INVALID_SNAPSHOT_CURSOR"}
            )
        if _guild_snapshot_cursor_changed(
            current_seq=current_snapshot_seq,
            current_generation=current_snapshot_generation,
            requested_seq=requested_snapshot_seq,
            requested_generation=requested_snapshot_generation,
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "KAED_FED_SNAPSHOT_CHANGED",
                    "snapshot_required": True,
                    "latest_seq": str(current_snapshot_seq),
                    "snapshot_generation": str(current_snapshot_generation),
                },
            )
        snapshot_seq = requested_snapshot_seq
        snapshot_at = member_snapshot_at
    else:
        snapshot_at = datetime.now(UTC)
    roles = list(
        await session.scalars(
            select(Role)
            .where(Role.guild_id == guild.id, Role.guild_domain == guild.origin_domain)
            .order_by(Role.position, Role.id)
            .limit(MAX_SNAPSHOT_VISIBILITY_ROLES + 1)
        )
    )
    if len(roles) > MAX_SNAPSHOT_VISIBILITY_ROLES:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})
    channels = await cached_visible_guild_channels_for_origin(
        session,
        redis,
        guild,
        principal.origin,
        loaded_roles=roles,
    )
    member_conditions = [
        GuildMember.guild_id == guild.id,
        GuildMember.guild_domain == guild.origin_domain,
        GuildMember.created_at <= snapshot_at,
    ]
    if member_cursor is not None:
        member_conditions.append(
            or_(
                GuildMember.user_domain > member_cursor[0],
                and_(
                    GuildMember.user_domain == member_cursor[0],
                    GuildMember.user_id > member_cursor[1],
                ),
            )
        )
    member_rows = (
        (
            await session.execute(
                select(GuildMember, User)
                .join(
                    User,
                    (User.id == GuildMember.user_id)
                    & (User.origin_domain == GuildMember.user_domain),
                )
                .where(*member_conditions)
                .order_by(GuildMember.user_domain, GuildMember.user_id)
                .limit(1001)
            )
        )
        .tuples()
        .all()
    )
    members = member_rows[:1000]
    next_member_cursor = (
        (members[-1][0].user_domain, members[-1][0].user_id) if len(member_rows) > 1000 else None
    )
    page_member_refs = {(member.user_id, member.user_domain) for member, _user in members}
    member_roles = (
        list(
            await session.scalars(
                select(MemberRole)
                .where(
                    MemberRole.guild_id == guild.id,
                    MemberRole.guild_domain == guild.origin_domain,
                    tuple_(MemberRole.user_id, MemberRole.user_domain).in_(page_member_refs),
                )
                .limit(100_001)
            )
        )
        if page_member_refs
        else []
    )
    if len(member_roles) > 100_000:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})
    channel_refs = {(channel.id, channel.origin_domain) for channel in channels}
    overwrites = list(
        await session.scalars(
            select(ChannelOverwrite)
            .where(
                ChannelOverwrite.channel_id.in_([ref[0] for ref in channel_refs]),
                ChannelOverwrite.channel_domain == guild.origin_domain,
            )
            .order_by(
                ChannelOverwrite.channel_id,
                ChannelOverwrite.target_type,
                ChannelOverwrite.target_id,
            )
            .limit(MAX_SNAPSHOT_VISIBILITY_OVERWRITES + 1)
        )
    )
    if len(overwrites) > MAX_SNAPSHOT_VISIBILITY_OVERWRITES:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})
    thread_members = (
        list(
            await session.scalars(
                select(ThreadMember)
                .where(
                    ThreadMember.thread_id.in_([ref[0] for ref in channel_refs]),
                    ThreadMember.thread_domain == guild.origin_domain,
                    tuple_(ThreadMember.user_id, ThreadMember.user_domain).in_(page_member_refs),
                )
                .order_by(
                    ThreadMember.thread_id,
                    ThreadMember.user_domain,
                    ThreadMember.user_id,
                )
                .limit(100_001)
            )
        )
        if channel_refs and page_member_refs
        else []
    )
    if len(thread_members) > 100_000:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})
    emojis = list(
        await session.scalars(
            select(Emoji)
            .where(Emoji.guild_id == guild.id, Emoji.guild_domain == guild.origin_domain)
            .order_by(Emoji.name, Emoji.id)
            .limit(1001)
        )
    )
    if len(emojis) > 1000:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})
    stickers = list(
        await session.scalars(
            select(Sticker)
            .where(Sticker.guild_id == guild.id, Sticker.guild_domain == guild.origin_domain)
            .order_by(Sticker.name, Sticker.id)
            .limit(1001)
        )
    )
    if len(stickers) > 1000:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_WORK_LIMIT"})
    payload = guild_snapshot_payload(
        guild,
        roles,
        channels,
        members,
        member_roles,
        overwrites,
        emojis=emojis,
        stickers=stickers,
        thread_members=thread_members,
        member_snapshot_at=snapshot_at,
        next_member_cursor=next_member_cursor,
        snapshot_seq=snapshot_seq,
    )
    await record_room_federation_recipient(
        session,
        ("guild", guild.id, guild.origin_domain),
        principal.origin,
    )
    await session.commit()
    return payload


@router.get("/_kaede/v1/guilds/{guild_id}/events")
async def federation_guild_events(
    guild_id: Snowflake,
    after_seq: int = Query(ge=0, le=MAX_SNOWFLAKE),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-events",
        capacity=300,
        refill_per_minute=300,
    )
    guild = await home_guild(session, settings, guild_id, for_share=True)
    await require_origin_guild_member(session, guild, principal.origin)
    admitted = await session.scalar(
        select(
            func.pg_try_advisory_xact_lock(
                func.hashtextextended(
                    f"kaede-guild-visibility:{principal.origin}:{guild.origin_domain}:{guild.id}",
                    0,
                )
            )
        )
    )
    if admitted is not True:
        raise HTTPException(status_code=429, detail={"code": "KAED_FED_SNAPSHOT_BUSY"})
    events = list(
        await session.scalars(
            select(GuildEvent)
            .where(
                GuildEvent.guild_id == guild.id,
                GuildEvent.guild_domain == guild.origin_domain,
                GuildEvent.seq > after_seq,
            )
            .order_by(GuildEvent.seq)
            .limit(1000)
        )
    )
    latest_seq = guild.next_event_seq - 1
    if guild_history_requires_snapshot(
        after_seq=after_seq,
        latest_seq=latest_seq,
        first_retained_seq=events[0].seq if events else None,
    ):
        raise HTTPException(
            status_code=410,
            detail={
                "code": "KAED_FED_FULL_RESYNC",
                "snapshot_required": True,
                "latest_seq": str(latest_seq),
            },
        )
    visible_channel_refs = {
        (channel.id, channel.origin_domain)
        for channel in await cached_visible_guild_channels_for_origin(
            session,
            redis,
            guild,
            principal.origin,
        )
    }
    rendered_events: list[dict[str, object]] = []
    owner: User | None = None
    for event in events:
        envelope = event.envelope
        event_type = str(envelope.get("type", ""))
        channel_ref = guild_event_channel_ref(envelope)
        hidden_channel_event = (
            channel_ref is not None
            and channel_ref not in visible_channel_refs
            and event_type != "guild.channel.delete"
        )
        if not hidden_channel_event:
            rendered_events.append(envelope)
            continue
        if owner is None:
            owner = await session.get(User, (guild.owner_id, guild.owner_domain))
        if owner is None or owner.origin_domain != settings.domain:
            raise RuntimeError("guild owner cannot sign a redacted gap-fill event")
        rendered_events.append(
            await build_envelope(
                session,
                settings,
                "guild.event.redacted",
                owner,
                {"original_type": event_type},
                context={
                    "guild_id": str(guild.id),
                    "guild_domain": guild.origin_domain,
                    "seq": str(event.seq),
                    **(
                        {
                            "snapshot_generation": str(
                                envelope.get("context", {}).get("snapshot_generation")
                            )
                        }
                        if isinstance(envelope.get("context"), dict)
                        and envelope["context"].get("snapshot_generation") is not None
                        else {}
                    ),
                    **(
                        {"snapshot_required": True}
                        if guild_event_requires_snapshot(envelope)
                        else {}
                    ),
                },
            )
        )
    disclosed_refs: set[tuple[int, str]] = set()
    for rendered_event in rendered_events:
        disclosed_refs.update(message_attachment_refs(rendered_event))
    _disclosed, terminal_wakes, terminal_refs = await record_disclosed_attachment_recipients(
        session,
        settings,
        disclosed_refs,
        principal.origin,
        room_ref=("guild", int(guild_id), settings.domain),
    )
    rendered_events = await _redact_terminal_guild_events(
        session,
        settings,
        int(guild_id),
        rendered_events,
        terminal_refs,
    )
    await session.commit()
    for destination in terminal_wakes:
        await enqueue_best_effort(federation_deliver, destination)
    return {
        "events": rendered_events,
        "latest_seq": str(latest_seq),
    }


@router.post("/_kaede/v1/guilds/{guild_id}/proxy")
async def federation_guild_proxy(
    guild_id: Snowflake,
    payload: GuildProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    room_ref = ("guild", int(guild_id), settings.domain)
    await lock_terminal_room(session, *room_ref)
    incoming_attachment_refs = attachment_refs_from_payloads([{"attachments": payload.attachments}])
    for attachment_id, attachment_domain in sorted(
        incoming_attachment_refs, key=lambda ref: (ref[1], ref[0])
    ):
        await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    if (
        incoming_attachment_refs
        and await session.scalar(
            select(MediaTombstoneSource.attachment_id)
            .where(
                tuple_(
                    MediaTombstoneSource.attachment_id,
                    MediaTombstoneSource.attachment_domain,
                ).in_(incoming_attachment_refs)
            )
            .limit(1)
        )
        is not None
    ):
        raise HTTPException(status_code=410, detail={"code": "ATTACHMENT_DELETED"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    await require_remote_user_creation_allowed(session, actor)
    guild = await home_guild(session, settings, guild_id, for_update=True)
    channel = await session.get(Channel, (int(payload.channel_id), guild.origin_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.guild_id != guild.id
        or channel.type not in {0, 5, 10, 11, 12}
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    require_message_encryption_policy(
        channel,
        content=payload.content,
        e2ee=payload.e2ee,
        attachment_count=len(payload.attachments),
    )
    needed = Permission.VIEW_CHANNEL | (
        Permission.SEND_MESSAGES_IN_THREADS
        if channel.type in {10, 11, 12}
        else Permission.SEND_MESSAGES
    )
    if payload.attachments:
        needed |= Permission.ATTACH_FILES
    actor_permissions = await require_permissions(
        session,
        redis,
        guild,
        actor,
        needed,
        channel=channel,
    )
    if (
        channel.type in {10, 11, 12}
        and channel.locked
        and not actor_permissions & Permission.MANAGE_THREADS
    ):
        raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})
    if (
        channel.type in {10, 11, 12}
        and actor.account_type == "bot"
        and (channel.e2ee_required or channel.encryption_mode == "e2ee")
    ):
        raise HTTPException(status_code=409, detail={"code": "BOT_THREAD_E2EE_UNSUPPORTED"})
    await record_room_federation_recipient(
        session,
        ("guild", guild.id, guild.origin_domain),
        principal.origin,
    )
    await validate_custom_emoji_use(
        session,
        actor,
        payload.content,
        target_guild=guild,
        target_permissions=actor_permissions,
        trust_unknown_external=True,
    )
    await lock_proxy_nonce(session, guild, actor, channel, payload.client_nonce)
    existing = await session.scalar(
        select(Message).where(
            Message.channel_id == channel.id,
            Message.channel_domain == channel.origin_domain,
            Message.author_id == actor.id,
            Message.author_domain == actor.origin_domain,
            Message.client_nonce == payload.client_nonce,
        )
    )
    if existing is not None:
        existing_event = await guild_event_for_message(session, guild, existing)
        if existing_event is None:
            raise HTTPException(status_code=409, detail={"code": "KAED_GUILD_NONCE_STATE_CONFLICT"})
        stored_content = existing_event.envelope.get("content")
        stored_message = stored_content.get("message") if isinstance(stored_content, dict) else None
        if not isinstance(stored_message, dict):
            raise HTTPException(status_code=409, detail={"code": "KAED_GUILD_NONCE_STATE_CONFLICT"})
        stored_attachment_refs = message_attachment_refs(existing_event.envelope)
        for attachment_id, attachment_domain in sorted(
            stored_attachment_refs, key=lambda ref: (ref[1], ref[0])
        ):
            await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
        if (
            stored_attachment_refs
            and await session.scalar(
                select(MediaTombstoneSource.attachment_id)
                .where(
                    tuple_(
                        MediaTombstoneSource.attachment_id,
                        MediaTombstoneSource.attachment_domain,
                    ).in_(stored_attachment_refs)
                )
                .limit(1)
            )
            is not None
        ):
            raise HTTPException(status_code=410, detail={"code": "ATTACHMENT_DELETED"})
        return {
            "message": stored_message,
            "seq": str(existing_event.seq),
            "event": existing_event.envelope,
        }
    prior_thread_message_projection = (
        capture_thread_message_projection(channel) if channel.type in {10, 11, 12} else None
    )
    thread_was_unarchived = False
    if channel.type in {10, 11, 12} and channel.archived:
        if channel.locked and not actor_permissions & Permission.MANAGE_THREADS:
            raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})
        await require_active_thread_capacity(
            session,
            guild,
            excluding=(channel.id, channel.origin_domain),
        )
        channel.archived = False
        channel.archive_timestamp = datetime.now(UTC)
        thread_was_unarchived = True
    referenced_message: Message | None = None
    if payload.referenced_message_id is not None:
        reference = payload.referenced_message_id.resolve(settings.domain)
        referenced_message = await session.get(Message, reference)
        if referenced_message is None or (
            referenced_message.channel_id,
            referenced_message.channel_domain,
        ) != (channel.id, channel.origin_domain):
            raise HTTPException(status_code=400, detail={"code": "INVALID_MESSAGE_REFERENCE"})
    if (
        channel.rate_limit_per_user
        and actor.account_type != "bot"
        and not actor_permissions & Permission.BYPASS_SLOWMODE
    ):
        slowmode_key = (
            f"slowmode:{channel.origin_domain}:{channel.id}:{actor.origin_domain}:{actor.id}"
        )
        allowed = await redis.set(
            slowmode_key,
            "1",
            ex=channel.rate_limit_per_user,
            nx=True,
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "SLOWMODE_RATE_LIMITED",
                    "retry_after_ms": max(1000, await redis.pttl(slowmode_key)),
                },
            )
    role_mention_refs = set(
        await role_mention_recipients(session, guild, payload.content, actor_permissions)
    )
    parsed_mention_refs = merge_mention_recipients(
        [item.resolve(principal.origin) for item in payload.mention_user_ids],
        list(role_mention_refs),
    )
    mention_refs = await validated_guild_mentions(session, guild, parsed_mention_refs)
    message = Message(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=actor.id,
        author_domain=actor.origin_domain,
        content=payload.content,
        e2ee=payload.e2ee,
        encryption_policy_generation=channel.encryption_policy_generation,
        encryption_epoch=channel.encryption_epoch,
        client_nonce=payload.client_nonce,
        referenced_message_id=(referenced_message.id if referenced_message is not None else None),
        referenced_message_domain=(
            referenced_message.origin_domain if referenced_message is not None else None
        ),
        mention_user_refs=mention_refs,
        flags=(0 if actor_permissions & Permission.EMBED_LINKS else 4),
    )
    session.add(message)
    await session.flush()
    added_thread_members: list[ThreadMember] = []
    thread_rekeyed = False
    if channel.type in {10, 11, 12}:
        channel.message_count = int(channel.message_count or 0) + 1
        channel.total_message_sent = int(channel.total_message_sent or 0) + 1
        channel.last_activity_at = message.created_at
        (
            added_thread_members,
            thread_rekeyed,
            failed_role_mentions,
        ) = await admit_thread_message_members(
            session,
            redis,
            settings,
            guild,
            channel,
            actor,
            actor_permissions,
            parsed_mention_refs,
            role_mention_refs,
        )
        if failed_role_mentions:
            message.flags |= MESSAGE_FLAG_FAILED_TO_MENTION_SOME_ROLES_IN_THREAD
        if thread_was_unarchived or thread_rekeyed:
            if prior_thread_message_projection is None:
                raise RuntimeError("thread message projection was not captured")
            await queue_guild_mutation(
                session,
                settings,
                guild,
                actor,
                "guild.channel.update",
                {
                    "channel": thread_structural_state_before_message(
                        channel,
                        prior_thread_message_projection,
                    )
                },
                channel=channel,
            )
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=message.channel_id,
            channel_domain=message.channel_domain,
            mention_user_refs=message.mention_user_refs,
        )
    )
    attachments = await replicate_message_attachments(
        session, settings, message, actor, payload.attachments
    )
    await advance_channel_cursor(session, channel, message.id, message.origin_domain)
    rendered = message_payload(message, actor, attachments)
    seq = await assign_guild_sequence(session, guild)
    owner = await session.get(User, (guild.owner_id, guild.owner_domain))
    if owner is None or owner.origin_domain != settings.domain:
        raise RuntimeError("guild owner cannot sign the authoritative message event")
    committed = await build_envelope(
        session,
        settings,
        "guild.message.committed",
        owner,
        {
            "message": rendered,
            "author": profile_from_user(actor),
            "thread_starter": False,
        },
        context={
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "seq": str(seq),
        },
    )
    store_guild_event(session, guild, seq, str(committed["event_id"]), committed)
    remote_destinations = await remote_destinations_with_channel_access(
        session, settings, guild, channel
    )
    for destination in remote_destinations:
        await queue_event(session, settings, destination, committed)
    await session.commit()
    if channel.type in {10, 11, 12}:
        thread_topic = guild_topic(guild.origin_domain, guild.id)
        await publish_dispatch(
            redis,
            thread_topic,
            "THREAD_UPDATE",
            channel_payload(channel),
        )
        if thread_was_unarchived:
            await publish_current_thread_member_updates(session, redis, guild, channel)
        if added_thread_members:
            rendered_members = [thread_member_payload(member) for member in added_thread_members]
            rich_rendered_members = [
                await rich_thread_member_payload(session, member) for member in added_thread_members
            ]
            for rendered_member in rendered_members:
                target_ref = f"{rendered_member['user_id']}@{rendered_member['user_domain']}"
                await publish_dispatch(
                    redis,
                    thread_topic,
                    "THREAD_CREATE",
                    channel_payload(channel) | {"member": rendered_member},
                    audience_user_refs=[target_ref],
                )
                await publish_dispatch(
                    redis,
                    thread_topic,
                    "THREAD_MEMBER_UPDATE",
                    rendered_member,
                    audience_user_refs=[target_ref],
                )
            await publish_dispatch(
                redis,
                thread_topic,
                "THREAD_MEMBERS_UPDATE",
                {
                    "id": str(channel.id),
                    "thread_domain": channel.origin_domain,
                    "guild_id": str(guild.id),
                    "guild_domain": guild.origin_domain,
                    "member_count": min(50, int(channel.member_count or 0)),
                    "added_members": rich_rendered_members,
                    "removed_member_ids": [],
                },
            )
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "MESSAGE_CREATE", rendered
    )
    await enqueue_best_effort(mentions_fanout, message.id, message.origin_domain)
    for destination in remote_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    return {"message": rendered, "seq": str(seq), "event": committed}


@router.post("/_kaede/v1/guilds/{guild_id}/proxy-pin")
async def federation_guild_pin_proxy(
    guild_id: Snowflake,
    payload: GuildPinProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    """Apply a remote member's pin mutation at the authoritative guild home."""
    require_guild_federation_access(principal)
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    guild = await home_guild(session, settings, guild_id, for_update=True)
    channel = await session.get(Channel, (int(payload.channel_id), guild.origin_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.guild_id != guild.id
        or channel.type not in {0, 5, 10, 11, 12}
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if channel.type in {10, 11, 12} and channel.archived:
        raise HTTPException(status_code=409, detail={"code": "THREAD_ARCHIVED"})
    await require_permissions(
        session,
        redis,
        guild,
        actor,
        Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY | Permission.PIN_MESSAGES,
        channel=channel,
    )
    message_id, message_domain = payload.message_id.resolve(settings.domain)
    message = await session.get(Message, (message_id, message_domain))
    if (
        message is None
        or message.deleted_at is not None
        or (message.channel_id, message.channel_domain) != (channel.id, channel.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    changed = False
    if payload.pinned:
        inserted = await session.scalar(
            pg_insert(Pin)
            .values(
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                message_id=message.id,
                message_domain=message.origin_domain,
                pinned_by_id=actor.id,
                pinned_by_domain=actor.origin_domain,
            )
            .on_conflict_do_nothing()
            .returning(Pin.message_id)
        )
        changed = inserted is not None
    else:
        removed = await session.scalar(
            delete(Pin)
            .where(
                Pin.channel_id == channel.id,
                Pin.channel_domain == channel.origin_domain,
                Pin.message_id == message.id,
                Pin.message_domain == message.origin_domain,
            )
            .returning(Pin.message_id)
        )
        changed = removed is not None
    if changed:
        # The guild home signs authoritative mutations. Keep the remote member
        # who performed the action in the event content, as reactions do.
        owner = await session.get(User, (guild.owner_id, guild.owner_domain))
        if owner is None or not owner.is_local or owner.origin_domain != settings.domain:
            raise RuntimeError("local guild owner cannot sign the authoritative pin event")
        await queue_guild_mutation(
            session,
            settings,
            guild,
            owner,
            "guild.pin.add" if payload.pinned else "guild.pin.remove",
            {
                "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                "channel": {"id": str(channel.id), "origin_domain": channel.origin_domain},
                "user": {"id": str(actor.id), "origin_domain": actor.origin_domain},
            },
            channel=channel,
        )
    await session.commit()
    if changed:
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "MESSAGE_UPDATE",
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "pinned": payload.pinned,
            },
        )
    return {"pinned": payload.pinned}


@router.post("/_kaede/v1/guilds/{guild_id}/proxy-reaction")
async def federation_guild_reaction_proxy(
    guild_id: Snowflake,
    payload: GuildReactionProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    """Apply a remote member's reaction at the authoritative guild home."""
    require_guild_federation_access(principal)
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    guild = await home_guild(session, settings, guild_id, for_update=True)
    channel = await session.get(Channel, (int(payload.channel_id), guild.origin_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.guild_id != guild.id
        or channel.type not in {0, 5, 10, 11, 12}
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if channel.type in {10, 11, 12} and channel.archived:
        raise HTTPException(status_code=409, detail={"code": "THREAD_ARCHIVED"})
    needed = Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY
    actor_permissions = await require_permissions(
        session,
        redis,
        guild,
        actor,
        needed,
        channel=channel,
    )
    await validate_custom_emoji_use(
        session,
        actor,
        payload.emoji,
        target_guild=guild,
        target_permissions=actor_permissions,
        trust_unknown_external=True,
    )
    message_id, message_domain = payload.message_id.resolve(settings.domain)
    message = await session.get(Message, (message_id, message_domain))
    if (
        message is None
        or message.deleted_at is not None
        or (message.channel_id, message.channel_domain) != (channel.id, channel.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    if not payload.remove:
        emoji_exists = bool(
            await session.scalar(
                select(
                    exists().where(
                        Reaction.message_id == message.id,
                        Reaction.message_domain == message.origin_domain,
                        Reaction.emoji_key == payload.emoji,
                    )
                )
            )
        )
        if not emoji_exists and not actor_permissions & Permission.ADD_REACTIONS:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    changed = False
    if payload.remove:
        removed = await session.scalar(
            delete(Reaction)
            .where(
                Reaction.message_id == message.id,
                Reaction.message_domain == message.origin_domain,
                Reaction.user_id == actor.id,
                Reaction.user_domain == actor.origin_domain,
                Reaction.emoji_key == payload.emoji,
            )
            .returning(Reaction.message_id)
        )
        changed = removed is not None
    else:
        inserted = await session.scalar(
            pg_insert(Reaction)
            .values(
                message_id=message.id,
                message_domain=message.origin_domain,
                user_id=actor.id,
                user_domain=actor.origin_domain,
                emoji_key=payload.emoji,
            )
            .on_conflict_do_nothing()
            .returning(Reaction.message_id)
        )
        changed = inserted is not None
    if changed:
        # Federation envelopes emitted by a guild authority must be signed by
        # one of that authority's local users.  The remote reactor remains the
        # semantic actor in the event content, while the local guild owner is
        # the authority signer (the same pattern used by committed proxy
        # messages).  Passing the remote user to queue_guild_mutation makes
        # build_envelope correctly reject the guild home for signing on behalf
        # of another instance.
        owner = await session.get(User, (guild.owner_id, guild.owner_domain))
        if owner is None or not owner.is_local or owner.origin_domain != settings.domain:
            raise RuntimeError("local guild owner cannot sign the authoritative reaction event")
        await queue_guild_mutation(
            session,
            settings,
            guild,
            owner,
            "guild.reaction.remove" if payload.remove else "guild.reaction.add",
            {
                "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                "user": {"id": str(actor.id), "origin_domain": actor.origin_domain},
                "emoji": payload.emoji,
            },
            channel=channel,
        )
    await session.commit()
    if changed:
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "MESSAGE_UPDATE",
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "reaction": payload.emoji,
                "removed": payload.remove,
                "user_id": str(actor.id),
                "user_domain": actor.origin_domain,
            },
        )
    return {"reacted": not payload.remove}
