from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import secrets
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import reduce
from operator import or_ as bit_or
from typing import Any, Literal, cast

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
from redis.exceptions import RedisError
from sqlalchemy import and_, delete, exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.bot_federation import bootstrap_runtime_application_projection
from app.api.calls import notify_call
from app.api.channels import (
    ANNOUNCEMENT_FOLLOW_LIFECYCLE_EVENTS,
    MESSAGE_FLAG_FAILED_TO_MENTION_SOME_ROLES_IN_THREAD,
    AnnouncementCopyViewProjection,
    MessageMutationOptions,
    _clear_reactions,
    accept_federated_announcement_follow_source,
    add_encrypted_poll_rows,
    add_poll_vote,
    add_reaction,
    admit_thread_message_members,
    apply_announcement_copy_projection,
    apply_announcement_follow_lifecycle_event,
    apply_announcement_follower_attribution,
    authorize_federated_announcement_follow_source,
    authorize_federated_announcement_follow_target,
    bulk_delete_messages,
    capture_thread_message_projection,
    crosspost_message,
    deactivate_federated_announcement_follow_source,
    delete_announcement_follow,
    delete_message,
    edit_message,
    encrypted_rich_routing,
    ensure_poll_result_message,
    finalize_poll,
    follow_announcement_channel,
    list_announcement_follows,
    list_channel_pins,
    list_poll_voters,
    list_reaction_users,
    lock_announcement_mutation,
    mark_guild_activity,
    persist_pin_notice,
    pin_message,
    publish_current_thread_member_updates,
    record_pin_audit_entry,
    remove_own_reaction,
    remove_poll_vote,
    remove_user_reaction,
    require_encrypted_rich_admission,
    require_forward_age_context,
    require_message_encryption_policy,
    require_owned_e2ee_sender_device,
    require_pinnable_channel,
    require_typing_access,
    require_voice_message_attachments,
    resolve_encrypted_rich_mention_projection,
    revoke_federated_announcement_follow_target,
    stored_announcement_follow_projection,
    sync_target_announcement_copy_view,
    thread_structural_state_before_message,
    unpin_message,
    validate_signed_forward_source_proof,
    validated_federated_announcement_follow_source_authorization,
)
from app.api.dependencies import AuthenticatedUser, get_redis, get_session, get_snowflake
from app.api.e2ee import (
    RoomActivationRequest,
    RoomProposalRequest,
    RoomRekeyActivationRequest,
    activate_room_encryption_attested,
    activate_room_rekey_attested,
    claim_local_bot_room_key_packages,
    claim_local_room_key_packages,
    propose_room_encryption,
    propose_room_rekey,
    require_room_policy_authority,
    room_encryption_operation_status_for_actor,
)
from app.api.scheduled_events import (
    active_scheduled_event_for_invite,
    scheduled_event_invite_payload,
)
from app.auth.instance_restrictions import (
    require_remote_user_creation_allowed,
    require_remote_user_join_allowed,
)
from app.auth.tokens import AccessGrant
from app.automod.service import (
    AutoModMessageBlocked,
    AutoModPostCommit,
    evaluate_member_profile,
    require_member_interactions_allowed,
)
from app.automod.service import (
    evaluate_message as evaluate_automod_message,
)
from app.bootstrap import MAX_ADVERTISED_OLD_KEYS
from app.bots.developer_projection import (
    apply_developer_team_snapshot,
    authority_attested_developer_team_snapshot,
)
from app.bots.dm_capability import (
    BotDMCapabilityAuthorityUnavailable,
    BotDMCapabilityPayload,
    BotDMCapabilityProofInvalid,
    BotDMCapabilitySourceRejected,
    apply_bot_dm_capability,
    authority_attested_bot_dm_capability,
    fence_bot_dm_capabilities_for_pair,
    require_capability_runtime_binding,
    require_stored_capability_runtime,
    validate_bot_dm_capability_at_source,
    validated_bot_dm_capability_context,
)
from app.bots.installations import (
    cleanup_installation_roles,
    publish_deleted_installation_roles,
    revoke_installations_for_guild_member,
    usable_guild_installation,
    usable_user_installation,
)
from app.bots.interaction_events import (
    authority_attested_interaction_response,
    queue_received_interaction_dispatch,
    wake_interaction_dispatch_outbox,
)
from app.bots.interaction_owners import USER_INSTALL_OWNER
from app.bots.runtime_control import (
    APPLICATION_RUNTIME_EVENT,
    ApplicationRuntimeSnapshot,
    application_runtime_snapshot_fingerprint,
    apply_application_runtime_control,
    durably_apply_application_runtime_proof,
    validate_application_runtime_proof,
)
from app.bots.target_contract import authority_attested_application_target
from app.bots.target_discovery import apply_application_target_snapshot
from app.chat.allowed_mentions import (
    EVERYONE_MENTION,
    everyone_mention_recipients,
    resolve_allowed_mentions_projection,
)
from app.chat.announcement_identity import (
    federated_crosspost_key,
    federated_follow_key,
    qualified_follow_ref,
)
from app.chat.channel_access import ChannelAccess, effective_channel_nsfw, load_channel_access
from app.chat.custom_emojis import (
    CUSTOM_EMOJI_PATTERN,
    custom_emoji_refs,
    validate_custom_emoji_tokens,
)
from app.chat.custom_stickers import sticker_item_payload
from app.chat.dm_mutations import (
    DM_MESSAGE_MUTATION_EVENTS,
    authority_attested_dm_message_mutation,
)
from app.chat.e2ee import (
    classify_channel_encryption_policy_update,
    validate_channel_encryption_policy,
)
from app.chat.e2ee_controls import (
    authority_attested_direct_dm_control,
    authority_attested_room_policy_change,
)
from app.chat.e2ee_membership import (
    pause_guild_e2ee_for_membership_change,
    pause_local_e2ee_for_device_change,
    publish_e2ee_policy_updates,
    queue_e2ee_policy_federation,
)
from app.chat.events import (
    guild_topic,
    interaction_response_dispatch_expired,
    publish_dispatch,
    publish_ephemeral,
    user_topic,
)
from app.chat.expression_authorization import expression_custom_emoji_tokens
from app.chat.expression_events import (
    publish_guild_emojis_update,
    publish_guild_stickers_update,
)
from app.chat.forwarding import (
    FORWARD_SOURCE_AUTHORIZATION_EVENT,
    build_forward_source_authorization_content,
    can_forward_between_age_contexts,
    forward_snapshot_custom_emoji_tokens,
    forward_snapshot_matches_attachments,
    forward_snapshot_sticker_items,
    validate_forward_snapshot_source_binding,
)
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
    build_guild_authority_envelope,
    guild_authority_owner,
    queue_guild_access_revocation,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.interaction_metadata import validate_interaction_metadata
from app.chat.invites import grant_invite_roles, invite_allows_user, invite_target_payload
from app.chat.mention_policy import regular_message_allowed_mentions
from app.chat.mentions import (
    merge_mention_recipients,
    role_mention_recipients,
    role_mention_refs,
)
from app.chat.message_flags import (
    MESSAGE_FLAG_HAS_SNAPSHOT,
    MESSAGE_FLAG_IS_COMPONENTS_V2,
    MESSAGE_FLAG_IS_CROSSPOST,
    MESSAGE_FLAG_IS_VOICE_MESSAGE,
    MESSAGE_FLAG_SUPPRESS_EMBEDS,
    MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS,
    inferred_message_shape_flags,
)
from app.chat.moderation_status import guild_self_moderation_status, sanitize_timeout_reason
from app.chat.payloads import (
    channel_payload,
    dm_channel_payload,
    guild_payload,
    member_payload,
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
from app.chat.pins import (
    CHANNEL_PIN_LIMIT,
    authority_attested_direct_pin_notice,
    channel_pin_count,
    channel_pins_update_payload,
    message_is_pinnable,
)
from app.chat.poll_results import (
    DM_POLL_MUTATION_EVENTS,
    authority_attested_direct_poll_result,
    authority_attested_dm_poll_mutation,
)
from app.chat.privacy import blocked_between, lock_dm_policy, require_can_direct_message
from app.chat.reaction_payloads import reaction_event_payload, reaction_payloads_for_messages
from app.chat.rich_content import message_automod_text, uses_components_v2
from app.chat.schemas import ChannelFollowCreate, MessageBulkDelete, ReactionCreate
from app.chat.thread_limits import require_active_thread_capacity
from app.chat.thread_membership import (
    RemovedThreadMembers,
    cleanup_guild_member_threads,
    publish_guild_thread_member_cleanup,
)
from app.chat.voice_messages import require_voice_message_guild_capacity
from app.core.channel_types import is_message_capable_channel_type
from app.core.dm import dm_authority_domain, dm_pair_key
from app.core.federation import (
    DURABLE_LATEST_STATE_EVENTS,
    FEDERATION_CAPABILITIES,
    GUILD_MUTATION_EVENT_TYPES,
    authority_attested_group_event_ref,
    authority_attested_guild_crosspost_actor,
    authority_attested_media_delete_ref,
    canonical_json,
    guild_authority_event_ref,
    guild_crosspost_authority_event_ref,
    guild_media_delete_request_ref,
    guild_message_authority_event_refs,
    terminal_room_event_ref,
    terminal_room_generation,
    verify_envelope,
)
from app.core.json_limits import strict_json_loads
from app.core.metrics import increment_metric
from app.core.permission_contract import required_permissions
from app.core.permissions import PERMISSION_SCHEMA, Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import MAX_SNOWFLAKE, EntityRef, Snowflake
from app.db.bot_models import (
    BotApplication,
    BotInstallation,
    BotUserInstallation,
    FederatedInteractionAdmissionGrant,
    FederatedInteractionResponseLocator,
)
from app.db.models import (
    Attachment,
    Ban,
    Channel,
    ChannelFollow,
    ChannelOverwrite,
    DMConversation,
    DMParticipant,
    E2EEControlRecord,
    Emoji,
    FederatedChannelFollow,
    FederatedMessageCrosspost,
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
    MessageCrosspost,
    MessageProjection,
    MessageView,
    PeerKey,
    Pin,
    Poll,
    PollAnswer,
    PollVote,
    Reaction,
    RemoteGuildMembershipIntent,
    RemoteMediaCache,
    RemoteMediaTombstone,
    Role,
    RoomFederationRecipient,
    Sticker,
    TerminalRoomDeletion,
    ThreadMember,
    TrackerBoard,
    TrackerLane,
    TrackerTask,
    User,
)
from app.federation.actor_intents import (
    actor_intent_for_authority,
    validate_human_actor_intent,
    validate_worker_actor_intent,
)
from app.federation.client import signed_request
from app.federation.delivery import FederationOutboxCapacityExceeded
from app.federation.dm_history import MAX_DM_HISTORY_RESPONSE_BYTES
from app.federation.dm_mutations import DMMutationResult, apply_dm_message_mutation
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
from app.federation.expression_authorization import (
    validate_attested_expression_target,
    validate_expression_authorization_map,
)
from app.federation.forwarding import validated_forward_source_proof
from app.federation.guilds import (
    HISTORY_ACCESS_MUTATION_EVENT_TYPES,
    REMOTE_GUILD_JOINING,
    GuildSequenceGap,
    _validated_message_rich_projection,
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
from app.federation.message_content import validate_webhook_attribution
from app.federation.network import (
    FederationInstanceQuotaExceeded,
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
    peer_key_needs_refresh,
)
from app.federation.poll_mutations import (
    DMPollMutationResult,
    apply_dm_poll_mutation,
)
from app.federation.presence import receive_presence
from app.federation.relationships import (
    GUILD_PROFILE_RELAY_EVENT,
    RelationshipApplication,
    RelationshipQuotaExceeded,
    apply_relationship_event,
    guild_profile_member_payload,
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
    resolve_delegated_profile,
    upsert_remote_user,
)
from app.federation.schemas import (
    AnnouncementCrosspostDeliverRequest,
    AnnouncementCrosspostResolveRequest,
    AnnouncementFollowAcceptRequest,
    AnnouncementFollowActorRequest,
    AnnouncementFollowAuthorizeRequest,
    AnnouncementFollowDeactivateRequest,
    AnnouncementFollowRevokeRequest,
    AnnouncementFollowSourceAuthorizeRequest,
    ChannelPinsPageProxyRequest,
    DMForwardResolveFederationRequest,
    DMGroupAuthorizeRequest,
    DMGroupMutationRequest,
    DMMessageOperationRequest,
    DMOpenFederationRequest,
    E2EEKeyPackageClaimRequest,
    E2EERoomOperationStatusRequest,
    E2EERoomProxyRequest,
    EventEnvelope,
    ForwardSourceAuthorizeFederationRequest,
    GuildForwardResolveRequest,
    GuildHistoryExportRequest,
    GuildJoinRequest,
    GuildLeaveRequest,
    GuildMessageOperationRequest,
    GuildPinProxyRequest,
    GuildPollFinalizeProxyRequest,
    GuildPollVoteProxyRequest,
    GuildPollVotersProxyRequest,
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
from app.federation.tracker import (
    MAX_TRACKER_LANES,
    MAX_TRACKER_PAGE_TASK_CANDIDATES,
    MAX_TRACKER_TASKS,
    TARGET_TRACKER_PAGE_BYTES,
    TrackerSnapshotChanged,
    tracker_snapshot_cursor_task_id,
    tracker_snapshot_page_payload,
    tracker_snapshot_page_size,
)
from app.federation.typing import (
    TypingProjection,
    TypingPublishRequest,
    TypingRelayRequest,
    accept_typing_generation,
    publish_authoritative_typing,
    publish_local_typing,
    typing_projection_is_fresh,
    validate_typing_relay_scope,
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
from app.tracker.membership import clear_tracker_assignees, wake_tracker_membership_cleanup
from app.tracker.outbox import wake_tracker_dispatch_outbox
from app.voice.rooms import parse_participant_identity, participant_identity
from app.voice.schemas import CallResponse
from app.voice.state import create_call, get_active_call, get_call, is_call_accepted

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
                        event_context=event.context,
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
                        break
                    result = await process_event(
                        session, redis, get_settings(), principal, event, snowflake
                    )
                    results.append(result.model_dump())
                    if session.in_transaction():
                        # Structural/signature rejections can return before the
                        # inbox path commits. Never retain their shared policy
                        # lock while waiting for the next WebSocket frame.
                        await session.rollback()
                    if result.status == "retry":
                        # A destination stream is ordered. Do not apply later
                        # mutations after a retryable head-of-line event; the
                        # sender will retry the omitted suffix behind it.
                        break
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


@dataclass(frozen=True, slots=True)
class DMOpenProjection:
    conversation: dict[str, object]
    channel: dict[str, object]
    participants: tuple[dict[str, object], ...]
    created_channel: dict[str, object] | None


async def materialize_dm_open_projection(
    session: AsyncSession,
    conversation: DMConversation,
    channel: Channel,
    *,
    local_recipient_ref: tuple[int, str],
    created: bool,
) -> DMOpenProjection:
    """Capture a DM-open response before server-managed channel state expires."""

    await session.flush()
    await session.refresh(conversation)
    await session.refresh(channel)
    participants = list(
        await session.scalars(
            select(User)
            .join(
                DMParticipant,
                (DMParticipant.user_id == User.id)
                & (DMParticipant.user_domain == User.origin_domain),
            )
            .where(
                DMParticipant.conversation_id == channel.id,
                DMParticipant.conversation_domain == channel.origin_domain,
            )
        )
    )
    return DMOpenProjection(
        conversation={
            "id": str(conversation.id),
            "origin_domain": conversation.origin_domain,
            "pair_key": conversation.pair_key,
            "authority_domain": conversation.authority_domain,
        },
        channel=channel_payload(channel),
        participants=tuple(profile_from_user(user) for user in participants),
        created_channel=(
            dm_channel_payload(
                channel,
                [
                    user
                    for user in participants
                    if (user.id, user.origin_domain) != local_recipient_ref
                ],
                conversation=conversation,
            )
            if created
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class ProxyMentionProjection:
    recipient_refs: tuple[tuple[int, str], ...]
    recipient_payload: tuple[dict[str, str], ...]
    role_refs: tuple[tuple[int, str], ...]
    role_recipients: frozenset[tuple[int, str]]
    everyone: bool


@dataclass(frozen=True, slots=True)
class ThreadDispatchProjection:
    guild_ref: tuple[int, str]
    channel: dict[str, object]
    added_members: tuple[tuple[str, dict[str, object], dict[str, object]], ...]
    members_update: dict[str, object] | None


async def materialize_thread_dispatch(
    session: AsyncSession,
    thread: Channel,
    added_members: Sequence[ThreadMember] = (),
) -> ThreadDispatchProjection:
    """Resolve server-managed thread state before its transaction commits."""

    await session.flush()
    await session.refresh(thread)
    if thread.guild_id is None or thread.guild_domain is None:
        raise RuntimeError("thread has no guild reference")
    rendered_members: list[tuple[str, dict[str, object], dict[str, object]]] = []
    for member in added_members:
        await session.refresh(member)
        rendered = thread_member_payload(member)
        rendered_members.append(
            (
                f"{rendered['user_id']}@{rendered['user_domain']}",
                rendered,
                await rich_thread_member_payload(session, member),
            )
        )
    return ThreadDispatchProjection(
        guild_ref=(int(thread.guild_id), str(thread.guild_domain)),
        channel=channel_payload(thread),
        added_members=tuple(rendered_members),
        members_update=(
            {
                "id": str(thread.id),
                "thread_domain": thread.origin_domain,
                "guild_id": str(thread.guild_id),
                "guild_domain": thread.guild_domain,
                "member_count": min(50, int(thread.member_count or 0)),
                "added_members": [rich for _target, _member, rich in rendered_members],
                "removed_member_ids": [],
            }
            if rendered_members
            else None
        ),
    )


PROXY_REQUEST_FINGERPRINT_VERSION = 1
PROXY_REQUEST_FINGERPRINT_KEYS = frozenset({"version", "sha256"})
PROXY_ATTACHMENT_IDENTITY_FIELDS = (
    "filename",
    "size",
    "encryption_mode",
    "encryption_protocol",
    "duration_secs",
    "waveform",
    "content_sha256",
)


class ProxyNonceStateConflict(ValueError):
    """A client nonce is already bound to different immutable semantics."""


class UnsupportedProxyFingerprintVersion(ProxyNonceStateConflict):
    """A durable proxy receipt uses a fingerprint contract this server cannot verify."""


def _qualified_proxy_ref(value: EntityRef | None, default_domain: str) -> str | None:
    if value is None:
        return None
    identifier, domain = value.resolve(default_domain)
    return f"{identifier}@{domain}"


def proxy_attachment_identity(raw: dict[str, object]) -> dict[str, object]:
    """Project stable attachment identity while ignoring processing metadata."""

    identity = {field: raw.get(field) for field in PROXY_ATTACHMENT_IDENTITY_FIELDS}
    identity.update(
        {
            "id": str(database_snowflake(raw.get("id"), "attachment id")),
            "origin_domain": normalize_domain(str(raw.get("origin_domain", ""))),
        }
    )
    return identity


def proxy_attachment_identity_key(identity: dict[str, object]) -> tuple[str, int]:
    return str(identity["origin_domain"]), int(str(identity["id"]))


def proxy_request_fingerprint(
    payload: GuildProxyRequest,
    authority_domain: str,
    *,
    version: int = PROXY_REQUEST_FINGERPRINT_VERSION,
) -> str:
    """Hash the immutable semantics of one guild proxy proposal.

    Authorization receipts are deliberately excluded: expression and forward
    proofs are short-lived and may be refreshed for an otherwise identical
    retry. Mutable actor profile fields are excluded as well, while the exact
    actor identity and account type remain bound. Presence of ``allowed_mentions``
    is retained because an omitted policy and an explicit null policy select
    different compatibility behavior for non-application messages.
    """

    if type(version) is not int or version != PROXY_REQUEST_FINGERPRINT_VERSION:
        raise UnsupportedProxyFingerprintVersion(
            f"unsupported guild proxy request fingerprint version {version}"
        )

    semantic = payload.model_dump(
        mode="json",
        exclude={"actor", "expression_authorizations", "forward_source_proof"},
    )
    semantic["actor_ref"] = f"{payload.actor.id}@{payload.actor.origin_domain}"
    semantic["actor_type"] = payload.actor.account_type
    semantic["allowed_mentions_present"] = "allowed_mentions" in payload.model_fields_set
    for field, default_domain in (
        ("application_id", payload.actor.origin_domain),
        ("referenced_message_id", authority_domain),
        ("forwarded_message_id", authority_domain),
        ("forwarded_channel_id", authority_domain),
        ("interaction_installation_ref", authority_domain),
    ):
        semantic[field] = _qualified_proxy_ref(getattr(payload, field), default_domain)
    semantic["mention_user_ids"] = [
        _qualified_proxy_ref(item, payload.actor.origin_domain) for item in payload.mention_user_ids
    ]
    if payload.allowed_mentions is not None:
        allowed = payload.allowed_mentions.model_dump(mode="json")
        allowed["users"] = [
            _qualified_proxy_ref(item, authority_domain) for item in payload.allowed_mentions.users
        ]
        allowed["roles"] = [
            _qualified_proxy_ref(item, authority_domain) for item in payload.allowed_mentions.roles
        ]
        semantic["allowed_mentions"] = allowed
    # Scan state, detected MIME, dimensions, blurhash, and variants can advance
    # after the source starts media processing.  They cannot turn an exact
    # retry into a collision.  The authority-only content digest *is* stable
    # identity and prevents different bytes from reusing the same nonce/ref.
    normalized_attachments = [proxy_attachment_identity(item) for item in payload.attachments]
    semantic["attachments"] = sorted(
        normalized_attachments,
        key=proxy_attachment_identity_key,
    )
    return hashlib.sha256(
        canonical_json(
            {
                "version": version,
                "proposal": semantic,
            },
            allow_floats=True,
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProxyRequestFingerprintReceipt:
    version: int
    sha256: str

    def wire(self) -> dict[str, object]:
        return {"version": self.version, "sha256": self.sha256}


def proxy_request_fingerprint_receipt(
    payload: GuildProxyRequest,
    authority_domain: str,
) -> ProxyRequestFingerprintReceipt:
    return ProxyRequestFingerprintReceipt(
        version=PROXY_REQUEST_FINGERPRINT_VERSION,
        sha256=proxy_request_fingerprint(payload, authority_domain),
    )


def _validated_proxy_fingerprint_digest(raw: object, *, label: str) -> str:
    if (
        not isinstance(raw, str)
        or len(raw) != 64
        or any(character not in "0123456789abcdef" for character in raw)
    ):
        raise ProxyNonceStateConflict(f"{label} fingerprint digest is invalid")
    return raw


def stored_proxy_request_fingerprint(event: GuildEvent) -> ProxyRequestFingerprintReceipt | None:
    content = event.envelope.get("content")
    raw = content.get("proxy_request_fingerprint") if isinstance(content, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != PROXY_REQUEST_FINGERPRINT_KEYS:
        raise ProxyNonceStateConflict("proxy commit fingerprint receipt is invalid")
    version = raw.get("version")
    if type(version) is not int:
        raise ProxyNonceStateConflict("proxy commit fingerprint version is invalid")
    if version != PROXY_REQUEST_FINGERPRINT_VERSION:
        raise UnsupportedProxyFingerprintVersion(
            f"unsupported guild proxy request fingerprint version {version}"
        )
    return ProxyRequestFingerprintReceipt(
        version=version,
        sha256=_validated_proxy_fingerprint_digest(
            raw.get("sha256"),
            label="proxy commit",
        ),
    )


@dataclass(frozen=True, slots=True)
class ProxyNonceReplay:
    message: Message
    event: GuildEvent


def message_proxy_fingerprint_receipt(
    message: Message,
) -> tuple[ProxyRequestFingerprintReceipt, int] | None:
    version = getattr(message, "proxy_request_fingerprint_version", None)
    digest = getattr(message, "proxy_request_fingerprint", None)
    commit_seq = getattr(message, "proxy_commit_seq", None)
    if version is None and digest is None:
        # A sequence without a fingerprint is an intentional migration state:
        # retention keeps the legacy event, while the old full projection
        # comparison remains responsible for deciding a retry.
        return None
    if type(version) is not int or digest is None or type(commit_seq) is not int:
        raise ProxyNonceStateConflict("proxy message has an incomplete durable nonce receipt")
    if version != PROXY_REQUEST_FINGERPRINT_VERSION:
        raise UnsupportedProxyFingerprintVersion(
            f"unsupported guild proxy request fingerprint version {version}"
        )
    if commit_seq < 1:
        raise ProxyNonceStateConflict("proxy message commit sequence is invalid")
    return (
        ProxyRequestFingerprintReceipt(
            version=version,
            sha256=_validated_proxy_fingerprint_digest(
                digest,
                label="proxy message",
            ),
        ),
        commit_seq,
    )


def bind_proxy_commit_receipt(
    message: Message,
    receipt: ProxyRequestFingerprintReceipt,
    seq: int,
) -> None:
    if seq < 1:
        raise ValueError("proxy commit sequence must be positive")
    message.proxy_request_fingerprint_version = receipt.version
    message.proxy_request_fingerprint = receipt.sha256
    message.proxy_commit_seq = seq


def validate_proxy_commit_event_identity(
    guild: Guild,
    message: Message,
    event: GuildEvent,
) -> None:
    context = event.envelope.get("context")
    content = event.envelope.get("content")
    stored_message = content.get("message") if isinstance(content, dict) else None
    if (
        event.guild_id != guild.id
        or event.guild_domain != guild.origin_domain
        or event.envelope.get("type") != "guild.message.committed"
        or not isinstance(context, dict)
        or str(context.get("guild_id")) != str(guild.id)
        or context.get("guild_domain") != guild.origin_domain
        or str(context.get("seq")) != str(event.seq)
        or not isinstance(stored_message, dict)
        or str(stored_message.get("id")) != str(message.id)
        or stored_message.get("origin_domain") != message.origin_domain
    ):
        raise ProxyNonceStateConflict("proxy message commit receipt references the wrong event")


def validate_proxy_commit_receipt_event(
    guild: Guild,
    message: Message,
    event: GuildEvent,
    expected: ProxyRequestFingerprintReceipt,
) -> None:
    validate_proxy_commit_event_identity(guild, message, event)
    stored = stored_proxy_request_fingerprint(event)
    if stored is None or stored != expected:
        raise ProxyNonceStateConflict("proxy message and event fingerprint receipts differ")


async def locked_proxy_nonce_replay(
    session: AsyncSession,
    guild: Guild,
    channel: Channel,
    actor: User,
    payload: GuildProxyRequest,
) -> ProxyNonceReplay | None:
    """Return a durable exact replay before consulting mutable guild state.

    Legacy committed events have no fingerprint and intentionally fall through
    to their former projection comparison path.
    """

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
    if existing is None:
        return None
    durable_receipt = message_proxy_fingerprint_receipt(existing)
    if durable_receipt is None:
        legacy_seq = getattr(existing, "proxy_commit_seq", None)
        if legacy_seq is not None:
            if type(legacy_seq) is not int or legacy_seq < 1:
                raise ProxyNonceStateConflict("legacy proxy message commit sequence is invalid")
            legacy_event = await session.get(
                GuildEvent,
                (guild.id, guild.origin_domain, legacy_seq),
                populate_existing=True,
            )
        else:
            legacy_event = await guild_event_for_message(session, guild, existing)
        if legacy_event is None:
            raise ProxyNonceStateConflict("legacy proxy message has no retained commit event")
        validate_proxy_commit_event_identity(guild, existing, legacy_event)
        return None
    stored, commit_seq = durable_receipt
    event = await session.get(
        GuildEvent,
        (guild.id, guild.origin_domain, commit_seq),
        populate_existing=True,
    )
    if event is None:
        raise ProxyNonceStateConflict("proxy message commit receipt event is missing")
    validate_proxy_commit_receipt_event(guild, existing, event, stored)
    requested = proxy_request_fingerprint(
        payload,
        guild.origin_domain,
        version=stored.version,
    )
    if not secrets.compare_digest(stored.sha256, requested):
        raise ProxyNonceStateConflict("proxy nonce replay changed immutable message fields")
    return ProxyNonceReplay(message=existing, event=event)


def queued_guild_proxy_request(envelope: EventEnvelope) -> GuildProxyRequest:
    """Normalize current and immediately preceding queued proxy wire shapes."""

    actor_raw = envelope.content.get("actor")
    if not isinstance(actor_raw, dict) or (
        str(actor_raw.get("id")) != envelope.actor.id
        or actor_raw.get("origin_domain") != envelope.actor.domain
    ):
        raise ValueError("proxy actor mismatch")
    normalized_proxy = dict(envelope.content)
    normalized_proxy.setdefault("operation", "message.create")
    if "referenced_message_id" not in normalized_proxy:
        legacy_reference = normalized_proxy.pop("referenced_message_ref", None)
        if isinstance(legacy_reference, dict):
            normalized_proxy["referenced_message_id"] = (
                f"{legacy_reference.get('id')}@{legacy_reference.get('origin_domain')}"
            )
    if "mention_user_ids" not in normalized_proxy:
        legacy_mentions = normalized_proxy.pop("mention_user_refs", [])
        if isinstance(legacy_mentions, list):
            normalized_proxy["mention_user_ids"] = [
                f"{item.get('id')}@{item.get('origin_domain')}"
                for item in legacy_mentions
                if isinstance(item, dict)
            ]
    try:
        return GuildProxyRequest.model_validate(normalized_proxy)
    except ValidationError as exc:
        raise ValueError("queued guild proxy write is invalid") from exc


async def queued_proxy_request_replay(
    session: AsyncSession,
    settings: Settings,
    envelope: EventEnvelope,
) -> tuple[GuildProxyRequest, ProxyNonceReplay | None]:
    """Resolve an inbox proxy retry against its message-owned commit receipt."""

    payload = queued_guild_proxy_request(envelope)
    if str(envelope.context.get("guild_domain")) != settings.domain:
        raise ValueError("proxy write was not addressed to this guild authority")
    guild = await home_guild(
        session,
        settings,
        database_snowflake(envelope.context.get("guild_id"), "guild id"),
        for_update=True,
    )
    channel = await session.get(Channel, (int(payload.channel_id), guild.origin_domain))
    actor = await session.get(
        User,
        (int(payload.actor.id), payload.actor.origin_domain),
    )
    if channel is None or channel.guild_id != guild.id or actor is None:
        return payload, None
    return payload, await locked_proxy_nonce_replay(session, guild, channel, actor, payload)


async def resolve_proxy_guild_mentions(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    channel: Channel,
    actor: User,
    actor_permissions: int,
    payload: GuildProxyRequest,
    *,
    referenced: Message | None,
) -> ProxyMentionProjection:
    """Resolve one proxy mention projection for direct and queued admission."""

    explicit = tuple(
        dict.fromkeys(item.resolve(actor.origin_domain) for item in payload.mention_user_ids)
    )
    encrypted = await resolve_encrypted_rich_mention_projection(
        session,
        ChannelAccess(channel=channel, guild=guild, participants=[]),
        payload.e2ee,
        actor_permissions=actor_permissions,
        referenced=referenced,
    )
    if encrypted is not None:
        if tuple(sorted(explicit)) != encrypted.recipients:
            raise HTTPException(status_code=409, detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"})
        recipients = list(encrypted.recipients)
        roles = encrypted.roles
        role_recipients = frozenset(encrypted.role_recipients)
        everyone = encrypted.everyone
    elif "allowed_mentions" in payload.model_fields_set or payload.application_id is not None:
        resolved = await resolve_allowed_mentions_projection(
            session,
            redis,
            settings,
            ChannelAccess(channel=channel, guild=guild, participants=[]),
            actor,
            regular_message_allowed_mentions(payload.allowed_mentions),
            payload.content,
            payload.components,
            actor_permissions=actor_permissions,
            replied_user_ref=(
                (referenced.author_id, referenced.author_domain) if referenced is not None else None
            ),
        )
        recipients = list(resolved.recipients)
        roles = resolved.roles
        role_recipients = frozenset(resolved.role_recipients)
        everyone = resolved.everyone
    else:
        visible_text = message_automod_text(
            payload.content,
            poll=payload.poll,
            components=payload.components,
        )
        roles = tuple(role_mention_refs(visible_text))
        role_recipients = frozenset(
            await role_mention_recipients(session, guild, visible_text, actor_permissions)
        )
        recipients = merge_mention_recipients(list(explicit), list(role_recipients))
        everyone = bool(isinstance(visible_text, str) and EVERYONE_MENTION.search(visible_text))
        if everyone:
            recipients = merge_mention_recipients(
                recipients,
                list(
                    await everyone_mention_recipients(
                        session,
                        ChannelAccess(channel=channel, guild=guild, participants=[]),
                        Permission(actor_permissions),
                    )
                ),
            )
    rendered = await validated_guild_mentions(session, guild, recipients)
    return ProxyMentionProjection(
        recipient_refs=tuple(recipients),
        recipient_payload=tuple(rendered),
        role_refs=tuple(roles),
        role_recipients=role_recipients,
        everyone=everyone,
    )


async def validated_proxy_application(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    application_ref: EntityRef | None,
    *,
    required_scope: str = "messages.send",
) -> tuple[int, str] | None:
    """Bind an interactive proxy message to its installed application.

    The authenticated actor home may select an application, but only the guild
    home may attest that the exact bot/application pair is actively installed.
    This prevents ordinary remote members (or a second bot) from claiming a
    component namespace that will later receive privileged interactions.
    """

    if application_ref is None:
        return None
    app_id, app_domain = application_ref.resolve(actor.origin_domain)
    if actor.account_type != "bot":
        raise HTTPException(status_code=403, detail={"code": "COMPONENT_APPLICATION_REQUIRED"})
    row = (
        await session.execute(
            select(BotApplication, BotInstallation)
            .join(
                BotInstallation,
                (BotInstallation.application_id == BotApplication.id)
                & (BotInstallation.application_domain == BotApplication.origin_domain),
            )
            .where(
                BotApplication.id == app_id,
                BotApplication.origin_domain == app_domain,
                BotApplication.bot_user_id == actor.id,
                BotApplication.bot_user_domain == actor.origin_domain,
                BotApplication.status == "active",
                BotInstallation.guild_id == guild.id,
                BotInstallation.guild_domain == guild.origin_domain,
                BotInstallation.bot_user_id == actor.id,
                BotInstallation.bot_user_domain == actor.origin_domain,
                usable_guild_installation(),
                BotInstallation.granted_scopes.contains([required_scope]),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    return app_id, app_domain


def requested_proxy_message_view_lineage(
    payload: GuildProxyRequest,
    authority_domain: str,
) -> tuple[str, int, str, int] | None:
    """Parse the source-authority lineage carried by an interactive proposal."""

    if payload.interaction_installation_ref is None:
        return None
    installation_id, installation_domain = payload.interaction_installation_ref.resolve(
        authority_domain
    )
    return (
        cast(str, payload.interaction_integration_type),
        installation_id,
        installation_domain,
        int(cast(str, payload.interaction_installation_revision)),
    )


def proxy_user_installation_owner(
    interaction_metadata: dict[str, object] | None,
    authority_domain: str,
) -> tuple[int, str] | None:
    """Read the authority-qualified user-install owner from validated metadata."""

    if not isinstance(interaction_metadata, dict):
        return None
    owners = interaction_metadata.get("authorizing_integration_owners")
    if not isinstance(owners, dict):
        return None
    raw_owner = owners.get(USER_INSTALL_OWNER)
    if not isinstance(raw_owner, str):
        return None
    try:
        return EntityRef(raw_owner).resolve(authority_domain)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class AuthoritativeProxyInteractionProjection:
    """Authority-normalized interaction attribution for a proxy message."""

    message_type: int
    metadata: dict[str, object] | None
    transport_lineage: tuple[str, int, str, int] | None
    installation_lineage: tuple[str, int, str, int] | None


async def authoritative_guild_message_view_lineage(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    application_ref: tuple[int, str] | None,
    requested: tuple[str, int, str, int] | None,
    interaction_metadata: dict[str, object] | None,
) -> tuple[str, int, str, int] | None:
    """Translate a source installation reference to the guild home's local FK.

    A peer's ``BotUserInstallation.id`` is a local surrogate and is never a
    portable identity.  Guild installation IDs are authority minted, but old
    senders paired them with their own domain.  Resolve both forms against the
    exact active authority-owned grant and persist only the guild home's local
    ID/domain.
    """

    if requested is None:
        return None
    if application_ref is None:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_LINEAGE_INVALID"})
    integration_type, requested_id, requested_domain, requested_revision = requested
    if integration_type == "guild_install":
        installation = await session.scalar(
            select(BotInstallation).where(
                BotInstallation.id == requested_id,
                BotInstallation.application_id == application_ref[0],
                BotInstallation.application_domain == application_ref[1],
                BotInstallation.guild_id == guild.id,
                BotInstallation.guild_domain == guild.origin_domain,
                BotInstallation.bot_user_id == actor.id,
                BotInstallation.bot_user_domain == actor.origin_domain,
                BotInstallation.grant_revision == requested_revision,
                usable_guild_installation(),
            )
        )
        if installation is None or requested_domain not in {
            guild.origin_domain,
            actor.origin_domain,
        }:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_LINEAGE_INVALID"},
            )
        return (
            integration_type,
            installation.id,
            guild.origin_domain,
            installation.grant_revision,
        )
    if integration_type == "user_install":
        owner_ref = proxy_user_installation_owner(interaction_metadata, guild.origin_domain)
        if owner_ref is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_LINEAGE_INVALID"},
            )
        installation = await session.scalar(
            select(BotUserInstallation).where(
                BotUserInstallation.application_id == application_ref[0],
                BotUserInstallation.application_domain == application_ref[1],
                BotUserInstallation.user_id == owner_ref[0],
                BotUserInstallation.user_domain == owner_ref[1],
                BotUserInstallation.grant_revision == requested_revision,
                BotUserInstallation.granted_scopes.contains(["applications.commands"]),
                BotUserInstallation.contexts.contains(["guild"]),
                usable_user_installation(current_instance_domain=settings.domain),
                or_(
                    (
                        (BotUserInstallation.source_id == requested_id)
                        & (BotUserInstallation.source_domain == requested_domain)
                    ),
                    (
                        BotUserInstallation.source_id.is_(None)
                        & (BotUserInstallation.id == requested_id)
                        & (BotUserInstallation.user_domain == requested_domain)
                    ),
                ),
            )
        )
        if installation is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_LINEAGE_INVALID"},
            )
        return (
            integration_type,
            installation.id,
            guild.origin_domain,
            installation.grant_revision,
        )
    # A guild channel cannot be authorized by a conversation-scoped DM grant.
    raise HTTPException(status_code=409, detail={"code": "INTERACTION_LINEAGE_INVALID"})


async def authoritative_proxy_interaction_projection(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    application_ref: tuple[int, str] | None,
    payload: GuildProxyRequest,
    referenced_message: Message | None,
) -> AuthoritativeProxyInteractionProjection:
    """Normalize attribution and translate lineage once for every proxy path."""

    message_type = (
        payload.interaction_message_type
        if payload.interaction_message_type is not None
        else 19
        if referenced_message is not None
        else 0
    )
    metadata = validate_interaction_metadata(
        payload.interaction_metadata,
        message_type=message_type,
        application_ref=application_ref,
        referenced_message_ref=(
            (referenced_message.id, referenced_message.origin_domain)
            if referenced_message is not None
            else None
        ),
    )
    transport_lineage = requested_proxy_message_view_lineage(payload, settings.domain)
    installation_lineage = await authoritative_guild_message_view_lineage(
        session,
        settings,
        guild,
        actor,
        application_ref,
        transport_lineage,
        metadata,
    )
    return AuthoritativeProxyInteractionProjection(
        message_type=message_type,
        metadata=metadata,
        transport_lineage=transport_lineage,
        installation_lineage=installation_lineage,
    )


async def require_proxy_bot_e2ee_participation(
    session: AsyncSession,
    guild: Guild,
    channel: Channel,
    actor: User,
    application_ref: tuple[int, str] | None,
    envelope: object,
) -> BotInstallation | None:
    """Bind an origin-attested bot proxy write to its guild MLS consent."""

    if actor.account_type != "bot" or channel.encryption_mode != "e2ee":
        return None
    if application_ref is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"},
        )
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == application_ref[0],
            BotInstallation.application_domain == application_ref[1],
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.bot_user_id == actor.id,
            BotInstallation.bot_user_domain == actor.origin_domain,
            usable_guild_installation(),
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    await require_owned_e2ee_sender_device(
        session,
        actor,
        envelope,
        authority_domain=guild.origin_domain,
        channel=channel,
        bot_installation_id=installation.id,
    )
    return installation


async def validated_proxy_forward(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    destination_channel: Channel,
    actor: User,
    forwarded_ref: EntityRef | None,
) -> Message | None:
    """Resolve a live, same-guild forward under the actor's current grants."""

    if forwarded_ref is None:
        return None
    source_id, source_domain = forwarded_ref.resolve(settings.domain)
    source = await session.get(Message, (source_id, source_domain))
    if source is None or source.deleted_at is not None or source.e2ee is not None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    source_channel = await session.get(Channel, (source.channel_id, source.channel_domain))
    if (
        source_channel is None
        or source_channel.unavailable
        or (source_channel.guild_id, source_channel.guild_domain) != (guild.id, guild.origin_domain)
        or source_channel.encryption_mode == "e2ee"
        or destination_channel.encryption_mode == "e2ee"
    ):
        raise HTTPException(status_code=409, detail={"code": "FORWARD_CONTEXT_UNSUPPORTED"})
    await require_permissions(
        session,
        redis,
        guild,
        actor,
        Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY,
        channel=source_channel,
    )
    await require_forward_age_context(session, source_channel, destination_channel)
    return source


async def require_attested_forward_age_context(
    session: AsyncSession,
    destination_channel: Channel,
    source_nsfw: bool,
) -> None:
    """Recheck a signed snapshot's age boundary at the destination authority."""

    if not can_forward_between_age_contexts(
        source_nsfw,
        await effective_channel_nsfw(session, destination_channel),
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "AGE_RESTRICTED_FORWARD_UNSUPPORTED"},
        )


def validate_dm_forward_age_context(
    content: dict[str, Any],
    raw_message: dict[str, Any],
) -> None:
    """Validate the signed source context before admitting a DM snapshot."""

    message_snapshots = raw_message.get("message_snapshots")
    has_forward_snapshot = raw_message.get("forward_snapshot") is not None or (
        isinstance(message_snapshots, list) and bool(message_snapshots)
    )
    has_forward_age_context = "forward_source_nsfw" in content
    forward_source_nsfw = content.get("forward_source_nsfw")
    if has_forward_snapshot:
        if not has_forward_age_context or not isinstance(forward_source_nsfw, bool):
            raise ValueError("DM forward age context is missing")
        # Private conversations are never age-restricted destinations.
        if forward_source_nsfw:
            raise ValueError("age-restricted messages cannot be forwarded to a DM")
    elif has_forward_age_context:
        raise ValueError("non-forward DM message contains an age context")


async def validate_dm_forward_source_proof(
    session: AsyncSession,
    settings: Settings,
    content: dict[str, Any],
    raw_message: dict[str, Any],
    channel: Channel,
    *,
    validation_time: datetime | None = None,
) -> None:
    """Require the source authority's live-read attestation for a DM forward."""

    raw_forward_id = raw_message.get("forwarded_message_id")
    raw_forward_domain = raw_message.get("forwarded_message_domain")
    has_forward = raw_forward_id is not None or raw_forward_domain is not None
    raw_proof = content.get("forward_source_proof")
    if not has_forward:
        if raw_proof is not None:
            raise ValueError("non-forward DM message contains a source proof")
        return
    if raw_forward_id is None or raw_forward_domain is None or raw_proof is None:
        raise ValueError("DM forward source proof is missing")
    raw_forward_channel_id = raw_message.get("forwarded_channel_id")
    raw_forward_channel_domain = raw_message.get("forwarded_channel_domain")
    if raw_forward_channel_id is None or raw_forward_channel_domain is None:
        raise ValueError("DM forward source channel is missing")
    author = RemoteUserProfile.model_validate(content.get("author"))
    source_message_ref = (
        f"{database_snowflake(raw_forward_id, 'forwarded message id')}@"
        f"{normalize_domain(str(raw_forward_domain))}"
    )
    source_channel_ref = (
        f"{database_snowflake(raw_forward_channel_id, 'forwarded channel id')}@"
        f"{normalize_domain(str(raw_forward_channel_domain))}"
    )
    application_id = raw_message.get("application_id")
    application_domain = raw_message.get("application_domain")
    if (application_id is None) != (application_domain is None):
        raise ValueError("DM forward application identity is incomplete")
    application_ref = (
        f"{database_snowflake(application_id, 'application id')}@"
        f"{normalize_domain(str(application_domain))}"
        if application_id is not None
        else None
    )
    e2ee = raw_message.get("e2ee")
    proof_device_id = (
        cast(str, e2ee.get("sender_device_id"))
        if author.account_type == "bot"
        and isinstance(e2ee, dict)
        and isinstance(e2ee.get("sender_device_id"), str)
        else None
    )
    nonce = raw_message.get("client_nonce")
    if not isinstance(nonce, str):
        raise ValueError("DM forward client nonce is missing")
    proof = await validated_forward_source_proof(
        session,
        settings,
        raw_proof,
        requester_ref=f"{author.id}@{author.origin_domain}",
        requester_type=author.account_type,
        source_message_ref=source_message_ref,
        source_channel_ref=source_channel_ref,
        destination_channel_ref=f"{channel.id}@{channel.origin_domain}",
        destination_encryption_mode=cast(Literal["plaintext", "e2ee"], channel.encryption_mode),
        nonce=nonce,
        application_ref=application_ref,
        e2ee_device_id=proof_device_id,
        validation_time=validation_time,
    )
    if proof["source_nsfw"] is not content.get("forward_source_nsfw"):
        raise ValueError("DM forward source age context is inconsistent")
    raw_snapshot = raw_message.get("forward_snapshot")
    encrypted_forward = bool(
        isinstance(e2ee, dict)
        and "rich_payload_digest" in e2ee
        and e2ee.get("forward_snapshot_digest") is not None
    )
    if encrypted_forward:
        if not isinstance(e2ee, dict):
            raise ValueError("encrypted DM forward envelope is invalid")
        proof_created_at = datetime.fromisoformat(cast(str, proof["source_created_at"]))
        envelope_created_at = datetime.fromisoformat(str(e2ee.get("forwarded_created_at")))
        raw_proof_edited_at = proof.get("source_edited_at")
        raw_envelope_edited_at = e2ee.get("forwarded_edited_at")
        proof_edited_at = (
            datetime.fromisoformat(cast(str, raw_proof_edited_at))
            if raw_proof_edited_at is not None
            else None
        )
        envelope_edited_at = (
            datetime.fromisoformat(str(raw_envelope_edited_at))
            if raw_envelope_edited_at is not None
            else None
        )
        if raw_snapshot is not None or any(
            (
                e2ee.get("forward_source_projection_digest") != proof["source_projection_digest"],
                envelope_created_at != proof_created_at,
                envelope_edited_at != proof_edited_at,
                e2ee.get("forwarded_flags") != proof["source_flags"],
                e2ee.get("forwarded_message_type") != proof["source_message_type"],
            )
        ):
            raise ValueError("encrypted DM forward source proof is inconsistent")
    elif proof["source_encryption_mode"] == "plaintext":
        if raw_snapshot != proof["source_snapshot"]:
            raise ValueError("plaintext DM forward source proof is inconsistent")
    else:
        if raw_snapshot is None:
            raise ValueError("decrypted DM forward source proof is inconsistent")
        try:
            require_disclosed_forward_snapshot_proof_binding(raw_snapshot, proof)
        except ValueError as exc:
            raise ValueError("decrypted DM forward source proof is inconsistent") from exc
    proof_sticker_items = cast(list[dict[str, object]], proof["source_sticker_items"])
    proof_custom_emoji_refs = cast(list[str], proof["source_custom_emoji_refs"])
    if raw_snapshot is not None:
        try:
            if (
                forward_snapshot_sticker_items(raw_snapshot) != proof_sticker_items
                or forward_snapshot_custom_emoji_tokens(raw_snapshot) != proof_custom_emoji_refs
            ):
                raise ValueError("DM forward expression proof is inconsistent")
        except ValueError as exc:
            raise ValueError("DM forward expression proof is inconsistent") from exc
    else:
        raw_sticker_items = raw_message.get("sticker_items", [])
        raw_custom_emoji_refs = (
            e2ee.get("message_custom_emoji_refs", []) if isinstance(e2ee, dict) else []
        )
        if not isinstance(raw_sticker_items, list) or not isinstance(raw_custom_emoji_refs, list):
            raise ValueError("DM forward expression projection is invalid")
        routed_stickers = {
            f"{item.get('id')}@{item.get('origin_domain')}": item
            for item in raw_sticker_items
            if isinstance(item, dict)
        }
        if any(
            routed_stickers.get(f"{item['id']}@{item['origin_domain']}") != item
            for item in proof_sticker_items
        ) or not set(proof_custom_emoji_refs).issubset(str(item) for item in raw_custom_emoji_refs):
            raise ValueError("DM forward expression proof is inconsistent")
    raw_attachments = raw_message.get("attachments")
    if not isinstance(raw_attachments, list):
        raise ValueError("DM forward attachments are invalid")
    source_attachment_count = len(cast(list[str], proof["source_attachment_refs"]))
    if (
        encrypted_forward or proof["source_encryption_mode"] == "e2ee"
    ) and source_attachment_count != len(raw_attachments):
        raise ValueError("DM forward attachment count is inconsistent")
    if (
        raw_snapshot is not None
        and raw_attachments
        and not forward_snapshot_matches_attachments(
            raw_snapshot,
            raw_attachments,
        )
    ):
        raise ValueError("DM forward attachment binding is invalid")


def require_disclosed_forward_snapshot_proof_binding(
    snapshot: object,
    proof: dict[str, object],
) -> dict[str, object]:
    """Validate an E2EE-source snapshot before a plaintext replica stores it."""

    return validate_forward_snapshot_source_binding(
        snapshot,
        source_projection_digest=proof["source_projection_digest"],
        source_created_at=proof["source_created_at"],
        source_edited_at=proof["source_edited_at"],
        source_flags=proof["source_flags"],
        source_message_type=proof["source_message_type"],
    )


async def proxy_message_matches_request(
    session: AsyncSession,
    message: Message,
    payload: GuildProxyRequest,
    *,
    application_ref: tuple[int, str] | None,
    forwarded_message: Message | None,
    mentions: ProxyMentionProjection,
    installation_lineage: tuple[str, int, str, int] | None = None,
) -> bool:
    """Return whether a nonce replay is byte-equivalent in immutable fields."""

    embeds = [item.model_dump(mode="json", exclude_none=True) for item in payload.embeds]
    components = [item.model_dump(mode="json", exclude_none=True) for item in payload.components]
    encrypted_rich = isinstance(payload.e2ee, dict) and "rich_payload_digest" in payload.e2ee
    encrypted_contract, encrypted_controls, encrypted_poll = encrypted_rich_routing(payload.e2ee)
    referenced_ref = (
        payload.referenced_message_id.resolve(message.channel_domain)
        if payload.referenced_message_id is not None
        else None
    )
    forwarded_ref = (
        payload.forwarded_message_id.resolve(message.channel_domain)
        if payload.forwarded_message_id is not None
        else None
    )
    forwarded_channel_ref = (
        payload.forwarded_channel_id.resolve(message.channel_domain)
        if payload.forwarded_channel_id is not None
        else None
    )
    if (
        bool(int(message.flags or 0) & MESSAGE_FLAG_IS_VOICE_MESSAGE) != payload.voice_message
        or (
            int(message.flags or 0)
            & (
                MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS
                | MESSAGE_FLAG_IS_VOICE_MESSAGE
                | MESSAGE_FLAG_IS_COMPONENTS_V2
            )
        )
        != (
            (payload.flags & MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS)
            | inferred_message_shape_flags(
                voice_message=payload.voice_message,
                components_v2=uses_components_v2(payload.components),
            )
            | (payload.flags & MESSAGE_FLAG_IS_COMPONENTS_V2 if encrypted_rich else 0)
        )
        or bool(message.tts) != payload.tts
        or message.content != payload.content
        or message.e2ee != payload.e2ee
        or list(message.embeds or []) != embeds
        or list(message.components or []) != components
        or list(message.sticker_items or []) != payload.sticker_items
        or int(message.message_type or 0)
        != (
            payload.interaction_message_type
            if payload.interaction_message_type is not None
            else 19
            if referenced_ref is not None
            else 0
        )
        or message.interaction_metadata != payload.interaction_metadata
        or (
            (message.application_id, message.application_domain)
            if message.application_id is not None
            else None
        )
        != application_ref
        or (
            (message.forwarded_message_id, message.forwarded_message_domain)
            if message.forwarded_message_id is not None
            else None
        )
        != forwarded_ref
        or (
            (message.forwarded_channel_id, message.forwarded_channel_domain)
            if message.forwarded_channel_id is not None
            else None
        )
        != forwarded_channel_ref
        or message.forward_snapshot != payload.forward_snapshot
        or (
            (message.referenced_message_id, message.referenced_message_domain)
            if message.referenced_message_id is not None
            else None
        )
        != referenced_ref
        or list(message.mention_user_refs or []) != list(mentions.recipient_payload)
        or list(message.mention_role_refs or [])
        != [
            {"id": str(role_id), "origin_domain": role_domain}
            for role_id, role_domain in mentions.role_refs
        ]
        or bool(message.mention_everyone) is not mentions.everyone
    ):
        return False
    poll = await session.get(Poll, (message.id, message.origin_domain))
    if encrypted_poll is not None:
        if (
            poll is None
            or poll.question != {"encrypted": True, "version": 1}
            or poll.allow_multiselect != encrypted_poll["allow_multiselect"]
            or poll.layout_type != 1
            or abs(
                (
                    poll.expires_at
                    - message.created_at
                    - timedelta(seconds=cast(int, encrypted_poll["duration_seconds"]))
                ).total_seconds()
            )
            > 2
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
        if [answer.answer_id for answer in answers] != encrypted_poll["answer_ids"]:
            return False
    elif payload.poll is None:
        if poll is not None:
            return False
    else:
        if poll is None:
            return False
        expected_expiry = message.created_at + timedelta(hours=payload.poll.duration)
        if (
            poll.question != payload.poll.question.model_dump(mode="json", exclude_none=True)
            or poll.allow_multiselect != payload.poll.allow_multiselect
            or poll.layout_type != payload.poll.layout_type
            or abs((poll.expires_at - expected_expiry).total_seconds()) > 2
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
        expected_answers = [
            (
                answer_id,
                answer.poll_media.text,
                (
                    answer.poll_media.emoji.model_dump(mode="json", exclude_none=True)
                    if answer.poll_media.emoji is not None
                    else None
                ),
            )
            for answer_id, answer in enumerate(payload.poll.answers, start=1)
        ]
        if [
            (answer.answer_id, answer.text, answer.emoji) for answer in answers
        ] != expected_answers:
            return False
    view = await session.get(MessageView, (message.id, message.origin_domain))
    if payload.components or encrypted_controls:
        if (
            view is None
            or application_ref is None
            or installation_lineage is None
            or (view.application_id, view.application_domain) != application_ref
            or (
                view.integration_type,
                view.installation_id,
                view.installation_domain,
                view.installation_revision,
            )
            != installation_lineage
            or view.version != 1
            or view.persistent != payload.view_persistent
        ):
            return False
        if not payload.view_persistent:
            if view.expires_at is None:
                return False
            expected_expiry = message.created_at + timedelta(
                seconds=(
                    cast(int, encrypted_contract["view_timeout_seconds"])
                    if encrypted_contract is not None
                    else payload.view_timeout_seconds or 900
                )
            )
            if abs((view.expires_at - expected_expiry).total_seconds()) > 2:
                return False
    elif view is not None:
        return False
    stored_attachments = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.message_id == message.id,
                Attachment.message_domain == message.origin_domain,
                Attachment.deleted_at.is_(None),
            )
            .order_by(Attachment.origin_domain, Attachment.id)
        )
    )
    try:
        requested_attachments = sorted(
            (proxy_attachment_identity(item) for item in payload.attachments),
            key=proxy_attachment_identity_key,
        )
    except (TypeError, ValueError):
        return False
    stored_attachment_identities = sorted(
        (
            proxy_attachment_identity(
                {
                    "id": str(item.id),
                    "origin_domain": item.origin_domain,
                    "filename": item.filename,
                    "size": item.size,
                    "encryption_mode": item.encryption_mode,
                    "encryption_protocol": item.encryption_protocol,
                    "duration_secs": item.duration_secs,
                    "waveform": item.waveform,
                    "content_sha256": item.content_sha256,
                }
            )
            for item in stored_attachments
        ),
        key=proxy_attachment_identity_key,
    )
    return stored_attachment_identities == requested_attachments


async def validate_proxied_sticker_items(
    session: AsyncSession,
    guild: Guild,
    actor_permissions: int,
    sticker_items: list[dict[str, object]],
    *,
    trusted_external_domain: str,
    authority_attested_items: list[dict[str, object]] | None = None,
) -> None:
    attested_by_ref = {
        (int(str(item["id"])), str(item["origin_domain"])): item
        for item in authority_attested_items or []
    }
    observed_attested_refs: set[tuple[int, str]] = set()
    for item in sticker_items:
        item_ref = (int(str(item["id"])), str(item["origin_domain"]))
        attested = attested_by_ref.get(item_ref)
        if attested is not None:
            if attested != item:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "CUSTOM_STICKER_INVALID"},
                )
            observed_attested_refs.add(item_ref)
        if item["origin_domain"] != guild.origin_domain:
            if attested is None and item["origin_domain"] != trusted_external_domain:
                raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_INVALID"})
            if not actor_permissions & Permission.USE_EXTERNAL_STICKERS:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "USE_EXTERNAL_STICKERS_REQUIRED"},
                )
            continue
        sticker = await session.get(Sticker, (int(str(item["id"])), guild.origin_domain))
        if (
            sticker is None
            or (sticker.guild_id, sticker.guild_domain) != (guild.id, guild.origin_domain)
            or sticker_item_payload(sticker) != item
        ):
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_INVALID"})
    if observed_attested_refs != set(attested_by_ref):
        raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_INVALID"})


async def validate_proxied_custom_emoji_tokens(
    session: AsyncSession,
    actor: User,
    tokens: list[str],
    *,
    guild: Guild,
    actor_permissions: int,
    trusted_external_domain: str,
    authority_attested_tokens: list[str] | None = None,
) -> None:
    """Trust exact source-proof tokens while rechecking target permissions."""

    attested = set(authority_attested_tokens or [])
    if not attested.issubset(tokens):
        raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
    ordinary = [token for token in tokens if token not in attested]
    await validate_custom_emoji_tokens(
        session,
        actor,
        ordinary,
        target_guild=guild,
        target_permissions=actor_permissions,
        trusted_external_domain=trusted_external_domain,
    )
    for token in sorted(attested):
        match = CUSTOM_EMOJI_PATTERN.fullmatch(token)
        if match is None:
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
        if match.group("domain") == guild.origin_domain:
            await validate_custom_emoji_tokens(
                session,
                actor,
                [token],
                target_guild=guild,
                target_permissions=actor_permissions,
                trusted_external_domain=trusted_external_domain,
            )
        elif not actor_permissions & Permission.USE_EXTERNAL_EMOJIS:
            raise HTTPException(
                status_code=403,
                detail={"code": "USE_EXTERNAL_EMOJIS_REQUIRED"},
            )


async def validate_attested_forward_expressions(
    session: AsyncSession,
    actor: User,
    guild: Guild,
    actor_permissions: int,
    *,
    e2ee: dict[str, object] | None,
    routed_sticker_items: list[dict[str, object]],
    forward_snapshot: dict[str, object] | None,
    forward_proof: dict[str, object],
    trusted_external_domain: str,
) -> None:
    """Match expression routing to the source proof and target permissions."""

    attested_sticker_items = cast(
        list[dict[str, object]],
        forward_proof["source_sticker_items"],
    )
    attested_custom_emoji_refs = cast(
        list[str],
        forward_proof["source_custom_emoji_refs"],
    )
    if forward_snapshot is not None:
        try:
            expression_sticker_items = forward_snapshot_sticker_items(forward_snapshot)
            expression_custom_emoji_refs = forward_snapshot_custom_emoji_tokens(forward_snapshot)
        except ValueError as exc:
            raise ValueError("proxy forward expression projection is invalid") from exc
        if (
            expression_sticker_items != attested_sticker_items
            or expression_custom_emoji_refs != attested_custom_emoji_refs
        ):
            raise ValueError("proxy forward expression proof is inconsistent")
    else:
        expression_sticker_items = routed_sticker_items
        raw_expression_custom_emoji_refs = (
            e2ee.get("message_custom_emoji_refs", []) if e2ee is not None else []
        )
        if not isinstance(raw_expression_custom_emoji_refs, list):
            raise ValueError("proxy forward expression projection is invalid")
        expression_custom_emoji_refs = [str(item) for item in raw_expression_custom_emoji_refs]
    await validate_proxied_sticker_items(
        session,
        guild,
        actor_permissions,
        expression_sticker_items,
        trusted_external_domain=trusted_external_domain,
        authority_attested_items=attested_sticker_items,
    )
    await validate_proxied_custom_emoji_tokens(
        session,
        actor,
        expression_custom_emoji_refs,
        guild=guild,
        actor_permissions=actor_permissions,
        trusted_external_domain=trusted_external_domain,
        authority_attested_tokens=attested_custom_emoji_refs,
    )


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
        "permission_schema": PERMISSION_SCHEMA,
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
    owner = await guild_authority_owner(session, settings, guild)
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
    await clear_tracker_assignees(
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


async def _apply_authoritative_e2ee_room_policy(
    session: AsyncSession,
    settings: Settings,
    envelope: EventEnvelope,
) -> Channel | None:
    """Apply one durable room-policy projection or ACK authenticated old state."""

    raw_channel_id = envelope.content.get("channel_id")
    raw_channel_domain = envelope.content.get("channel_domain")
    channel_id = database_snowflake(raw_channel_id, "E2EE policy channel id")
    if raw_channel_domain != envelope.origin:
        raise ValueError("E2EE room policy did not originate at its authority")
    raw_scope_value = envelope.context.get("scope")
    if not isinstance(raw_scope_value, dict):
        raise ValueError("E2EE room policy scope is invalid")
    raw_scope = cast(dict[str, object], raw_scope_value)
    scope_type = raw_scope.get("type")
    if scope_type not in {"guild", "dm"}:
        raise ValueError("E2EE room policy scope type is invalid")
    scope_id = database_snowflake(raw_scope.get("id"), "E2EE policy scope id")
    scope_domain = normalize_domain(str(raw_scope.get("domain", "")))
    incoming_policy = validate_channel_encryption_policy(envelope.content.get("encryption_policy"))

    loaded_channel = await session.get(Channel, (channel_id, envelope.origin))
    if loaded_channel is None or loaded_channel.unavailable:
        if scope_type == "guild" and scope_domain == envelope.origin:
            terminal = await session.get(
                TerminalRoomDeletion,
                ("guild", scope_id, scope_domain, settings.domain),
                populate_existing=True,
            )
            if terminal is not None:
                return None
        raise ValueError("E2EE room policy references an unknown channel")
    channel = loaded_channel

    if scope_type == "guild":
        if (
            channel.guild_id is None
            or channel.guild_domain is None
            or (channel.guild_id, channel.guild_domain) != (scope_id, scope_domain)
            or scope_domain != envelope.origin
        ):
            raise ValueError("E2EE room policy guild context does not match its channel")
    elif (
        channel.guild_id is not None
        or channel.type != 1
        or (channel.id, channel.origin_domain) != (scope_id, scope_domain)
    ):
        raise ValueError("E2EE room policy DM context does not match its channel")

    update_state = classify_channel_encryption_policy_update(
        channel,
        incoming_policy,
        label="E2EE room",
    )
    if update_state != "apply":
        return None

    actor_id = database_snowflake(envelope.actor.id, "E2EE policy actor id")
    actor_membership: GuildMember | DMParticipant | None
    if scope_type == "guild":
        actor_membership = await session.get(
            GuildMember,
            (scope_id, scope_domain, actor_id, envelope.actor.domain),
        )
    else:
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

    channel.encryption_mode = str(incoming_policy["mode"])
    channel.encryption_state = str(incoming_policy["state"])
    channel.encryption_policy_generation = int(incoming_policy["generation"])
    channel.encryption_protocol = incoming_policy["protocol"]
    channel.encryption_suite = incoming_policy["suite"]
    channel.encryption_group_id = incoming_policy["group_id"]
    channel.encryption_epoch = incoming_policy["epoch"]
    return channel


async def _require_available_local_projection_actor(
    session: AsyncSession,
    envelope: EventEnvelope,
    *,
    actor_id_label: str,
    projection_label: str,
    account_type: Literal["human", "bot"],
) -> User:
    """Resolve the local actor named by an authority-attested projection."""

    actor = await session.get(
        User,
        (
            database_snowflake(envelope.actor.id, actor_id_label),
            envelope.actor.domain,
        ),
    )
    if (
        actor is None
        or not actor.is_local
        or actor.account_type != account_type
        or actor.disabled_at is not None
    ):
        raise ValueError(f"{projection_label} actor is unavailable")
    return actor


async def _apply_application_runtime_event(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    envelope: EventEnvelope,
) -> list[Channel]:
    """Apply one signed application-runtime projection."""

    runtime_snapshot = ApplicationRuntimeSnapshot.model_validate(envelope.content)
    runtime_actor = await session.get(
        User,
        (
            database_snowflake(envelope.actor.id, "application runtime actor id"),
            envelope.actor.domain,
        ),
    )
    if runtime_actor is None or runtime_actor.is_local or runtime_actor.account_type != "bot":
        raise ValueError("application runtime snapshot actor is unavailable")
    _runtime_changed, policy_channels = await apply_application_runtime_control(
        session,
        redis,
        settings,
        envelope.origin,
        runtime_actor,
        runtime_snapshot,
    )
    return policy_channels


async def _apply_application_target_event(
    session: AsyncSession,
    settings: Settings,
    envelope: EventEnvelope,
) -> None:
    """Apply one authority-attested application target projection."""

    actor = await _require_available_local_projection_actor(
        session,
        envelope,
        actor_id_label="application target actor id",
        projection_label="application target snapshot",
        account_type="bot",
    )
    await apply_application_target_snapshot(
        session,
        settings,
        envelope.origin,
        actor,
        envelope.content,
    )


async def _apply_developer_team_event(
    session: AsyncSession,
    settings: Settings,
    envelope: EventEnvelope,
) -> None:
    """Apply one authority-attested developer-team projection."""

    actor = await _require_available_local_projection_actor(
        session,
        envelope,
        actor_id_label="developer team actor id",
        projection_label="developer team snapshot",
        account_type="human",
    )
    await apply_developer_team_snapshot(
        session,
        settings,
        envelope.origin,
        actor,
        envelope.content,
    )


async def _apply_bot_dm_capability_event(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    envelope: EventEnvelope,
) -> list[Channel]:
    """Apply one authority-attested bot DM capability projection."""

    capability = BotDMCapabilityPayload.model_validate(envelope.content)
    if settings.domain not in {
        capability.application.domain,
        capability.authority_domain,
    }:
        raise ValueError("bot DM capability was delivered to the wrong instance")
    capability_actor = await session.get(
        User,
        (capability.bot_user.id, capability.bot_user.domain),
    )
    actor_identity_invalid = capability_actor is not None and (
        capability_actor.account_type != "bot"
        or (capability_actor.id, capability_actor.origin_domain)
        != (int(envelope.actor.id), envelope.actor.domain)
    )
    active_actor_unavailable = capability.status == "active" and (
        capability_actor is None or capability_actor.disabled_at is not None
    )
    if actor_identity_invalid or active_actor_unavailable:
        raise ValueError("bot DM capability actor is unavailable")
    if capability.status == "active":
        await require_stored_capability_runtime(session, settings, capability)
    applied_capability, capability_changed = await apply_bot_dm_capability(
        session,
        snowflake,
        envelope,
        capability,
        runtime_admitted=capability.status == "active",
    )
    if not (
        capability_changed and capability.status != "active" and applied_capability is not None
    ):
        return []

    from app.bots.e2ee import revoke_bot_e2ee_access

    return await revoke_bot_e2ee_access(
        session,
        redis,
        settings,
        dm_capability_ids=(applied_capability.id,),
    )


async def _require_interaction_response_invoker(
    session: AsyncSession,
    settings: Settings,
    envelope: EventEnvelope,
) -> User:
    """Resolve the enabled local human receiving a private bot response."""

    if envelope.actor.domain != settings.domain:
        raise ValueError("interaction response was delivered to the wrong user home")
    invoker = await session.get(
        User,
        (
            database_snowflake(envelope.actor.id, "interaction response user id"),
            settings.domain,
        ),
    )
    if (
        invoker is None
        or not invoker.is_local
        or invoker.account_type != "human"
        or invoker.disabled_at is not None
    ):
        raise ValueError("interaction response invoker is unavailable")
    return invoker


async def _bind_interaction_response_grant(
    session: AsyncSession,
    envelope: EventEnvelope,
    invoker: User,
    *,
    interaction_id: int,
    expires_at: datetime,
    channel_ref: tuple[int, str],
    application_ref: tuple[int, str],
) -> None:
    """Lock and consume the exact admission grant for a private response."""

    grant = await session.scalar(
        select(FederatedInteractionAdmissionGrant)
        .where(
            FederatedInteractionAdmissionGrant.grant_id
            == str(envelope.content["response_grant_id"]),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    grant_identity = (
        envelope.origin,
        invoker.id,
        invoker.origin_domain,
        *channel_ref,
        *application_ref,
    )
    if (
        grant is None
        or (
            grant.authority_domain,
            grant.user_id,
            grant.user_domain,
            grant.channel_id,
            grant.channel_domain,
            grant.application_id,
            grant.application_domain,
        )
        != grant_identity
        or grant.expires_at <= datetime.now(UTC)
        or expires_at != grant.expires_at
        or (
            grant.interaction_id is not None
            and (grant.interaction_id, grant.interaction_domain)
            != (interaction_id, envelope.origin)
        )
    ):
        raise ValueError("interaction response admission grant is invalid")
    if grant.interaction_id is None:
        grant.interaction_id = interaction_id
        grant.interaction_domain = envelope.origin


def _validate_interaction_response_locator(
    locator: FederatedInteractionResponseLocator,
    *,
    immutable: tuple[int, str, int, str, int, str, int, int],
    incoming_revision: int,
    event_fingerprint: str,
    expires_at: datetime,
    operation: str,
) -> None:
    """Reject immutable identity conflicts and attempts to revive a tombstone."""

    stored_identity = (
        locator.interaction_id,
        locator.interaction_domain,
        locator.user_id,
        locator.user_domain,
        locator.channel_id,
        locator.channel_domain,
        locator.sequence,
        locator.response_type,
    )
    if stored_identity != immutable:
        raise ValueError("interaction response identity conflicts with its projection")
    if locator.expires_at != expires_at:
        raise ValueError("interaction response expiry is immutable")
    if incoming_revision == locator.revision and locator.event_fingerprint != event_fingerprint:
        raise ValueError("interaction response revision conflicts with its projection")
    if locator.deleted and operation != "DELETE":
        raise ValueError("interaction response tombstone cannot be revived")


async def _apply_interaction_response_locator(
    session: AsyncSession,
    envelope: EventEnvelope,
    invoker: User,
    *,
    interaction_id: int,
    response_id: int,
    incoming_revision: int,
    operation: str,
    expires_at: datetime,
    event_fingerprint: str,
    channel_ref: tuple[int, str],
) -> bool:
    """Advance the durable private-response high-water when it is newer."""

    locator = await session.scalar(
        select(FederatedInteractionResponseLocator)
        .where(
            FederatedInteractionResponseLocator.response_id == response_id,
            FederatedInteractionResponseLocator.response_domain == envelope.origin,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    immutable = (
        interaction_id,
        envelope.origin,
        invoker.id,
        invoker.origin_domain,
        *channel_ref,
        int(envelope.content["sequence"]),
        int(envelope.content["callback_type"]),
    )
    if locator is not None:
        _validate_interaction_response_locator(
            locator,
            immutable=immutable,
            incoming_revision=incoming_revision,
            event_fingerprint=event_fingerprint,
            expires_at=expires_at,
            operation=operation,
        )
    should_apply = locator is None or incoming_revision > locator.revision
    if locator is None:
        session.add(
            FederatedInteractionResponseLocator(
                response_id=response_id,
                response_domain=envelope.origin,
                interaction_id=interaction_id,
                interaction_domain=envelope.origin,
                user_id=invoker.id,
                user_domain=invoker.origin_domain,
                channel_id=immutable[4],
                channel_domain=immutable[5],
                sequence=immutable[6],
                response_type=immutable[7],
                revision=incoming_revision,
                event_fingerprint=event_fingerprint,
                deleted=operation == "DELETE",
                expires_at=expires_at,
            )
        )
    elif should_apply:
        locator.revision = incoming_revision
        locator.event_fingerprint = event_fingerprint
        locator.deleted = locator.deleted or operation == "DELETE"
    return should_apply


async def _apply_authoritative_interaction_response(
    session: AsyncSession,
    settings: Settings,
    envelope: EventEnvelope,
    attested: tuple[int, int, int, str],
) -> tuple[str, dict[str, object]] | None:
    """Validate, persist, and queue one private federated bot response."""

    interaction_id, response_id, incoming_revision, operation = attested
    invoker = await _require_interaction_response_invoker(session, settings, envelope)
    channel_id, channel_domain = str(envelope.content["channel_ref"]).rsplit("@", 1)
    application_id, application_domain = str(envelope.content["application_ref"]).rsplit("@", 1)
    channel_ref = (
        database_snowflake(channel_id, "interaction response channel id"),
        normalize_domain(channel_domain),
    )
    application_ref = (
        database_snowflake(application_id, "interaction response application id"),
        normalize_domain(application_domain),
    )
    expires_at = datetime.fromisoformat(str(envelope.content["expires_at"]))
    event_fingerprint = hashlib.sha256(canonical_json(envelope.content)).hexdigest()
    await _bind_interaction_response_grant(
        session,
        envelope,
        invoker,
        interaction_id=interaction_id,
        expires_at=expires_at,
        channel_ref=channel_ref,
        application_ref=application_ref,
    )
    should_apply = await _apply_interaction_response_locator(
        session,
        envelope,
        invoker,
        interaction_id=interaction_id,
        response_id=response_id,
        incoming_revision=incoming_revision,
        operation=operation,
        expires_at=expires_at,
        event_fingerprint=event_fingerprint,
        channel_ref=channel_ref,
    )
    if not should_apply or expires_at <= datetime.now(UTC):
        return None
    await queue_received_interaction_dispatch(
        session,
        event_origin_domain=envelope.origin,
        event_id=envelope.event_id,
        user_id=invoker.id,
        user_domain=invoker.origin_domain,
        interaction_id=interaction_id,
        response_id=response_id,
        revision=incoming_revision,
        operation=cast(Literal["CREATE", "UPDATE", "DELETE"], operation),
        expires_at=expires_at,
    )
    return (
        f"INTERACTION_RESPONSE_{operation}",
        {str(key): value for key, value in envelope.content.items()},
    )


async def _preapply_bot_dm_runtime(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    envelope: EventEnvelope,
) -> tuple[bool, list[Channel]]:
    """Retain a valid bot runtime high-water outside DM event application."""

    runtime_work = await session.begin_nested()
    try:
        raw_runtime = envelope.content.get("bot_runtime_proof")
        runtime_envelope = EventEnvelope.model_validate(raw_runtime)
        runtime_snapshot = ApplicationRuntimeSnapshot.model_validate(runtime_envelope.content)
        if runtime_snapshot.target_domain != settings.domain:
            await runtime_work.commit()
            return False, []
        bot_ref = (
            database_snowflake(runtime_snapshot.bot_user_id, "bot runtime user id"),
            runtime_snapshot.bot_user_domain,
        )
        runtime_bot = await session.get(User, bot_ref)
        if runtime_bot is None:
            raw_profiles = envelope.content.get("participants")
            if not isinstance(raw_profiles, list):
                raise ValueError("bot DM runtime participants are missing")
            profiles = [RemoteUserProfile.model_validate(item) for item in raw_profiles]
            bot_profile = next(
                (
                    profile
                    for profile in profiles
                    if (int(profile.id), profile.origin_domain) == bot_ref
                ),
                None,
            )
            if bot_profile is None:
                raise ValueError("bot DM runtime actor is not a participant")
            runtime_bot = await upsert_remote_user(session, settings, bot_profile)
        if runtime_bot.account_type != "bot":
            raise ValueError("bot DM runtime actor is not a bot")
        validated_runtime_envelope, validated_runtime = await validate_application_runtime_proof(
            session,
            settings,
            expected_origin=runtime_snapshot.application_domain,
            raw_envelope=runtime_envelope,
            application_ref=(
                database_snowflake(
                    runtime_snapshot.application_id,
                    "bot runtime application id",
                ),
                runtime_snapshot.application_domain,
            ),
            bot_ref=bot_ref,
            target_domain=settings.domain,
        )
        if validated_runtime_envelope != runtime_envelope:
            raise ValueError("bot DM runtime proof changed during validation")
        _runtime_changed, runtime_channels = await apply_application_runtime_control(
            session,
            redis,
            settings,
            runtime_snapshot.application_domain,
            runtime_bot,
            validated_runtime,
            allow_target_bootstrap=True,
        )
        await runtime_work.commit()
        return True, runtime_channels
    except (HTTPException, TypeError, ValueError, ValidationError):
        await runtime_work.rollback()
        return False, []


async def _apply_e2ee_device_list_event(
    session: AsyncSession,
    settings: Settings,
    envelope: EventEnvelope,
) -> tuple[list[Channel], set[str]]:
    """Apply a remote device generation and route any resulting room fences."""

    profile = RemoteUserProfile.model_validate(envelope.content.get("profile"))
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
    if changed_user.e2ee_device_generation <= previous_generation:
        return [], set()

    paused_channels = await pause_local_e2ee_for_device_change(session, settings, changed_user)
    destinations: set[str] = set()
    for channel in paused_channels:
        destinations.update(
            await queue_e2ee_policy_federation(
                session,
                settings,
                changed_user,
                channel,
                authority_attested_actor=True,
            )
        )
    return paused_channels, destinations


async def _apply_group_call_event(
    session: AsyncSession,
    envelope: EventEnvelope,
) -> dict[str, Any]:
    """Validate and render one authority-committed group DM call."""

    call = CallResponse.model_validate(envelope.content.get("call"))
    if call.authority_domain != envelope.origin:
        raise ValueError("group call did not originate at its authority")
    if call.channel_id != str(
        envelope.context.get("conversation_id")
    ) or call.channel_domain != str(envelope.context.get("conversation_domain")):
        raise ValueError("group call context does not match its conversation")
    conversation = await session.get(
        DMConversation,
        (int(call.channel_id), call.channel_domain),
    )
    if conversation is None:
        raise FederationResyncRetry
    if (
        conversation.type != "group"
        or conversation.authority_domain != envelope.origin
        or conversation.origin_domain != envelope.origin
    ):
        raise ValueError("group call references a non-authoritative conversation")
    required_state_version = database_snowflake(
        envelope.context.get("state_version"),
        "group call state version",
    )
    if conversation.state_version < required_state_version:
        raise FederationResyncRetry
    caller_ref = parse_participant_identity(call.caller)
    if caller_ref != (
        database_snowflake(envelope.actor.id, "group call actor id"),
        envelope.actor.domain,
    ):
        raise ValueError("group call actor does not match its caller")
    current_participants = await group_participants(session, conversation)
    expected_identities = {
        participant_identity(user.id, user.origin_domain) for user in current_participants
    }
    if set(call.participants) != expected_identities:
        raise ValueError("group call participant set does not match group state")
    return call.model_dump(mode="json")


async def _ensure_replicated_group_call(
    redis: Redis,
    settings: Settings,
    call: dict[str, Any],
    identities: set[str],
) -> None:
    """Create an inbound call before acknowledgement, accepting an exact replay."""

    caller = str(call["caller"])
    if await create_call(redis, call, identities, settings, accepted={caller}):
        return
    authority = str(call["authority_domain"])
    call_id = int(str(call["id"]))
    existing, active, caller_accepted = await asyncio.gather(
        get_call(redis, authority, call_id),
        get_active_call(redis, str(call["channel_domain"]), int(str(call["channel_id"]))),
        is_call_accepted(redis, authority, call_id, caller),
    )
    if existing != call or active != call or not caller_accepted:
        raise ValueError("group call conflicts with active replica state")


def _is_transient_event_infrastructure_error(exc: BaseException) -> bool:
    """Recognize transport/storage outages that must never terminally reject an event."""

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (RedisError, OperationalError, InterfaceError)) or (
            isinstance(current, DBAPIError) and getattr(current.orig, "sqlstate", None) == "40P01"
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


async def process_event(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: FederationPrincipal,
    envelope: EventEnvelope,
    snowflake: SnowflakeGenerator,
) -> InboxResult:
    serialized_envelope = envelope.model_dump(mode="json")
    media_delete_ref = authority_attested_media_delete_ref(
        serialized_envelope,
        expected_authority=envelope.origin,
    )
    terminal_room_ref = terminal_room_event_ref(serialized_envelope)
    guild_media_request_ref = guild_media_delete_request_ref(serialized_envelope)
    authority_attested_terminal_guild = (
        terminal_room_ref is not None and terminal_room_ref[0] == "guild"
    )
    authority_attested_guild_owner = bool(
        envelope.actor.domain != principal.origin
        and (
            guild_authority_event_ref(
                envelope.type,
                envelope.context,
                expected_authority=envelope.origin,
            )
            is not None
            or guild_message_authority_event_refs(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=envelope.origin,
            )
            is not None
            or guild_crosspost_authority_event_ref(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=envelope.origin,
            )
            is not None
            or guild_media_request_ref is not None
        )
    )
    authority_attested_group = bool(
        envelope.actor.domain != principal.origin
        and (
            (terminal_room_ref is not None and terminal_room_ref[0] == "group_dm")
            or authority_attested_group_event_ref(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=envelope.origin,
                actor_id=envelope.actor.id,
                actor_domain=envelope.actor.domain,
            )
            is not None
        )
    )
    relayed_media_delete = envelope.type == "media.delete" and envelope.origin != principal.origin
    authority_attested_media_delete = bool(
        media_delete_ref is not None and envelope.actor.domain != envelope.origin
    )
    authority_attested_direct_control = authority_attested_direct_dm_control(
        envelope.type,
        envelope.content,
        expected_authority=envelope.origin,
        actor_id=envelope.actor.id,
        actor_domain=envelope.actor.domain,
    )
    authority_attested_pin_notice = authority_attested_direct_pin_notice(
        envelope.type,
        envelope.content,
        expected_authority=envelope.origin,
        actor=(envelope.actor.id, envelope.actor.domain),
    )
    authority_attested_poll_result = authority_attested_direct_poll_result(
        envelope.type,
        envelope.content,
        expected_authority=envelope.origin,
        actor=(envelope.actor.id, envelope.actor.domain),
    )
    authority_attested_dm_poll = authority_attested_dm_poll_mutation(
        envelope.type,
        envelope.content,
        envelope.context,
        expected_authority=envelope.origin,
    )
    authority_attested_dm_message = authority_attested_dm_message_mutation(
        envelope.type,
        envelope.content,
        envelope.context,
        expected_authority=envelope.origin,
        actor=(envelope.actor.id, envelope.actor.domain),
    )
    authority_attested_policy_change = authority_attested_room_policy_change(
        envelope.type,
        envelope.content,
        envelope.context,
        expected_authority=envelope.origin,
        actor_id=envelope.actor.id,
        actor_domain=envelope.actor.domain,
    )
    authority_attested_target_change = authority_attested_application_target(
        envelope.type,
        envelope.content,
        expected_authority=envelope.origin,
        actor=(envelope.actor.id, envelope.actor.domain),
    )
    authority_attested_team_snapshot = authority_attested_developer_team_snapshot(
        envelope.type,
        envelope.content,
        expected_authority=envelope.origin,
        actor=(envelope.actor.id, envelope.actor.domain),
    )
    authority_attested_bot_dm_grant = authority_attested_bot_dm_capability(
        envelope.type,
        envelope.content,
        expected_authority=envelope.origin,
        actor=(envelope.actor.id, envelope.actor.domain),
    )
    authority_attested_interaction = authority_attested_interaction_response(
        envelope.type,
        envelope.content,
        expected_authority=envelope.origin,
        actor=(envelope.actor.id, envelope.actor.domain),
    )
    if (envelope.origin != principal.origin and not relayed_media_delete) or (
        envelope.actor.domain != principal.origin
        and not authority_attested_group
        and not authority_attested_direct_control
        and not authority_attested_pin_notice
        and not authority_attested_poll_result
        and not authority_attested_dm_poll
        and not authority_attested_dm_message
        and not authority_attested_policy_change
        and not authority_attested_target_change
        and not authority_attested_team_snapshot
        and not authority_attested_bot_dm_grant
        and not authority_attested_interaction
        and not authority_attested_terminal_guild
        and not authority_attested_guild_owner
        and not authority_attested_media_delete
        and not relayed_media_delete
    ):
        return InboxResult(
            event_id=envelope.event_id,
            status="rejected",
            code="KAED_FED_AUTHOR_ORIGIN_MISMATCH",
        )
    if (
        envelope.type == "media.delete"
        and envelope.actor.domain != envelope.origin
        and media_delete_ref is None
    ):
        # A remote actor is attribution only on the exact removal-only proof.
        # Relays preserve that origin-signed envelope and never rewrite it.
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
            or envelope.type in DURABLE_LATEST_STATE_EVENTS
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
    # Counter updates need NO KEY UPDATE, which still serializes quota writers
    # while allowing local FederationEvent/FederationOutbox foreign-key checks
    # to take KEY SHARE. FOR UPDATE here can deadlock with a local publisher
    # that already owns an event-specific row lock.
    global_ledger = await session.scalar(
        select(Instance).where(Instance.is_self.is_(True)).with_for_update(key_share=True)
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
        .with_for_update(key_share=True)
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
    e2ee_policy_channels: list[Channel] = []
    bot_dm_runtime_preapplied = False
    if envelope.type in {"dm.open.request", "dm.conversation.create"}:
        # Preserve a valid A runtime high-water outside the event savepoint.
        # Capability, privacy, quota, or conversation rejection may roll back
        # below, but it must not make this instance forget a newer disable or
        # access epoch it has already authenticated.
        bot_dm_runtime_preapplied, runtime_channels = await _preapply_bot_dm_runtime(
            session,
            redis,
            settings,
            envelope,
        )
        e2ee_policy_channels.extend(runtime_channels)

    async def commit_inbox_state() -> None:
        """Commit SQL and project any pre-savepoint runtime policy once."""

        await session.commit()
        if not e2ee_policy_channels:
            return
        committed_channels = list(e2ee_policy_channels)
        e2ee_policy_channels.clear()
        try:
            await publish_e2ee_policy_updates(
                session,
                redis,
                settings,
                committed_channels,
            )
        except Exception:
            # SQL is authoritative and already committed. Reconnect/bootstrap
            # rehydrates policy if this best-effort live projection fails.
            log.exception(
                "federation_runtime_policy_projection_failed",
                origin=envelope.origin,
                event_id=envelope.event_id,
            )

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
                datetime.fromisoformat(str(envelope.content["expires_at"]))
                if authority_attested_interaction
                else None
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
            await commit_inbox_state()
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
    replicated_guild_member_role_ids: list[str] = []
    replicated_guild_dispatch: tuple[str, dict[str, object]] | None = None
    announcement_sync_guild: Guild | None = None
    announcement_sync_payload: dict[str, object] | None = None
    replicated_tracker_dispatch_queued = False
    home_message = None
    home_message_attachments: list[Attachment] = []
    home_message_created = False
    home_thread: Channel | None = None
    home_thread_members_added: list[ThreadMember] = []
    home_thread_updated = False
    home_thread_was_unarchived = False
    home_automod_post_commit = AutoModPostCommit()
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
    interaction_response_dispatch: tuple[str, dict[str, object]] | None = None
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
    replicated_group_call_identities: set[str] | None = None
    replicated_dm_poll_mutation: DMPollMutationResult | None = None
    replicated_dm_message_mutation: DMMutationResult | None = None
    authoritative_profile_relay_guild: Guild | None = None
    profile_member_dispatch: tuple[Guild, dict[str, object]] | None = None
    durably_committed = False
    try:
        queued_proxy_request_value: GuildProxyRequest | None = None
        proxy_nonce_replay: ProxyNonceReplay | None = None
        if envelope.type == "guild.proxy.message.create":
            try:
                queued_proxy_request_value, proxy_nonce_replay = await queued_proxy_request_replay(
                    session, settings, envelope
                )
            except ProxyNonceStateConflict as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "KAED_GUILD_NONCE_STATE_CONFLICT"},
                ) from exc
        if proxy_nonce_replay is not None:
            # A distinct signed queue event can be the sender's recovery after
            # the first authority commit raced a lost response. Re-deliver the
            # exact retained authority event to that actor home and perform no
            # second admission, projection, counter, AutoMod, or gateway work.
            await queue_event(
                session,
                settings,
                envelope.origin,
                proxy_nonce_replay.event.envelope,
                requeue_existing=True,
            )
            delivery_wakes.add(envelope.origin)
        elif guild_media_request_ref is not None:
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
        elif envelope.type == APPLICATION_RUNTIME_EVENT:
            e2ee_policy_channels.extend(
                await _apply_application_runtime_event(session, redis, settings, envelope)
            )
        elif authority_attested_target_change:
            await _apply_application_target_event(session, settings, envelope)
        elif authority_attested_team_snapshot:
            await _apply_developer_team_event(session, settings, envelope)
        elif authority_attested_bot_dm_grant:
            e2ee_policy_channels.extend(
                await _apply_bot_dm_capability_event(
                    session,
                    redis,
                    settings,
                    snowflake,
                    envelope,
                )
            )
        elif authority_attested_interaction:
            interaction_response_dispatch = await _apply_authoritative_interaction_response(
                session,
                settings,
                envelope,
                authority_attested_interaction,
            )
        elif envelope.type in DM_POLL_MUTATION_EVENTS:
            replicated_dm_poll_mutation = await apply_dm_poll_mutation(
                session,
                settings,
                event_type=envelope.type,
                content=envelope.content,
                context=envelope.context,
                event_origin=envelope.origin,
                actor_ref=(
                    database_snowflake(envelope.actor.id, "DM poll actor id"),
                    normalize_domain(envelope.actor.domain),
                ),
                event_timestamp_ms=envelope.ts,
            )
        elif envelope.type in DM_MESSAGE_MUTATION_EVENTS:
            replicated_dm_message_mutation = await apply_dm_message_mutation(
                session,
                settings,
                event_type=envelope.type,
                content=envelope.content,
                context=envelope.context,
                event_origin=envelope.origin,
                actor_ref=(
                    database_snowflake(envelope.actor.id, "DM mutation actor id"),
                    normalize_domain(envelope.actor.domain),
                ),
                event_timestamp_ms=envelope.ts,
            )
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
            if envelope.type == "relationship.remove":
                e2ee_policy_channels.extend(
                    await fence_bot_dm_capabilities_for_pair(
                        session,
                        redis,
                        settings,
                        relationship_application.recipient,
                        relationship_application.actor,
                    )
                )
            if relationship_application.wake_destination is not None:
                delivery_wakes.add(relationship_application.wake_destination)
        elif envelope.type == "guild.member.profile":
            if set(envelope.context) != {"guild_id", "guild_domain"}:
                raise ValueError("guild member profile context is invalid")
            if set(envelope.content) != {"actor"}:
                raise ValueError("guild member profile content is invalid")
            profile = RemoteUserProfile.model_validate(envelope.content["actor"])
            if envelope.origin != envelope.actor.domain or (profile.id, profile.origin_domain) != (
                envelope.actor.id,
                envelope.actor.domain,
            ):
                raise ValueError("guild member profile is not authoritative for its actor")
            guild_domain = normalize_domain(str(envelope.context["guild_domain"]))
            guild_id = database_snowflake(envelope.context["guild_id"], "guild id")
            profile_actor_ref = (
                database_snowflake(profile.id, "user id"),
                profile.origin_domain,
            )
            if guild_domain == settings.domain:
                profile_guild = await home_guild(
                    session,
                    settings,
                    guild_id,
                    for_update=True,
                )
                profile_member = await session.get(
                    GuildMember,
                    (
                        profile_guild.id,
                        profile_guild.origin_domain,
                        profile_actor_ref[0],
                        profile_actor_ref[1],
                    ),
                )
                # A delayed public-profile event after a leave is an accepted
                # no-op; it must not retain or rebroadcast former membership.
                if profile_member is not None:
                    existing_actor = await session.get(User, profile_actor_ref)
                    previous_version = (
                        existing_actor.profile_version if existing_actor is not None else 0
                    )
                    changed_actor = await upsert_remote_user(session, settings, profile)
                    await require_remote_user_creation_allowed(session, changed_actor)
                    if changed_actor.profile_version > previous_version:
                        home_automod_post_commit.extend(
                            await evaluate_member_profile(
                                session,
                                settings,
                                snowflake,
                                profile_guild,
                                changed_actor,
                            )
                        )
                        await queue_guild_mutation(
                            session,
                            settings,
                            profile_guild,
                            changed_actor,
                            GUILD_PROFILE_RELAY_EVENT,
                            {"source": envelope.model_dump(mode="json")},
                            pause_e2ee=False,
                        )
                        authoritative_profile_relay_guild = profile_guild
                        profile_member_dispatch = (
                            profile_guild,
                            await guild_profile_member_payload(
                                session,
                                profile_guild,
                                changed_actor,
                            ),
                        )
            else:
                replica_profile_guild = await session.get(Guild, (guild_id, guild_domain))
                if replica_profile_guild is None and await remote_guild_snapshot_is_pending(
                    session,
                    settings,
                    guild_id,
                    guild_domain,
                ):
                    raise FederationResyncRetry
                if replica_profile_guild is not None and not replica_profile_guild.unavailable:
                    profile_member = await session.get(
                        GuildMember,
                        (
                            replica_profile_guild.id,
                            replica_profile_guild.origin_domain,
                            profile_actor_ref[0],
                            profile_actor_ref[1],
                        ),
                    )
                    local_member = await session.scalar(
                        select(GuildMember.user_id)
                        .where(
                            GuildMember.guild_id == replica_profile_guild.id,
                            GuildMember.guild_domain == replica_profile_guild.origin_domain,
                            GuildMember.user_domain == settings.domain,
                        )
                        .limit(1)
                    )
                    if profile_member is not None and local_member is not None:
                        existing_actor = await session.get(User, profile_actor_ref)
                        previous_version = (
                            existing_actor.profile_version if existing_actor is not None else 0
                        )
                        changed_actor = await upsert_remote_user(session, settings, profile)
                        await require_remote_user_creation_allowed(session, changed_actor)
                        if changed_actor.profile_version > previous_version:
                            profile_member_dispatch = (
                                replica_profile_guild,
                                await guild_profile_member_payload(
                                    session,
                                    replica_profile_guild,
                                    changed_actor,
                                ),
                            )
        elif envelope.type == "e2ee.device-list.changed":
            paused_channels, destinations = await _apply_e2ee_device_list_event(
                session,
                settings,
                envelope,
            )
            e2ee_policy_channels.extend(paused_channels)
            delivery_wakes.update(destinations)
        elif envelope.type == "e2ee.room-policy.changed":
            if not authority_attested_policy_change:
                raise ValueError("E2EE room policy context is not authority-bound")
            changed_channel = await _apply_authoritative_e2ee_room_policy(
                session,
                settings,
                envelope,
            )
            if changed_channel is not None:
                e2ee_policy_channels.append(changed_channel)
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
                {
                    "participants": envelope.content["participants"],
                    "bot_capability": envelope.content.get("bot_capability"),
                    "bot_runtime_proof": envelope.content.get("bot_runtime_proof"),
                }
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
            bot_capability = await _authorize_bot_dm_open_capability(
                session,
                redis,
                snowflake,
                settings,
                open_request,
                [local_user, remote_user],
                relay_domain=envelope.origin,
                pair_key=pair_key,
                authority_domain=authority,
                runtime_preapplied=bot_dm_runtime_preapplied,
                commit_runtime=False,
            )
            if bot_capability is None and not await has_outbound_dm_open_request(
                session, envelope.origin, pair_key, local_user
            ):
                await require_can_direct_message(session, remote_user, local_user)
            created_dm_channel = await replicate_conversation(
                session, settings, raw_conversation, profiles
            )
            if bot_capability is not None:
                capability_conversation = await session.get(
                    DMConversation,
                    (created_dm_channel.id, created_dm_channel.origin_domain),
                )
                if capability_conversation is None:
                    raise RuntimeError("replicated bot DM conversation disappeared")
                await apply_bot_dm_capability(
                    session,
                    snowflake,
                    bot_capability[0],
                    bot_capability[1],
                    conversation=capability_conversation,
                    runtime_admitted=True,
                    admit_fenced_projection=True,
                )
            dm_channel_recipient = local_user
        elif envelope.type == "dm.open.request":
            open_request = DMOpenFederationRequest.model_validate(
                {
                    "participants": envelope.content["participants"],
                    "bot_capability": envelope.content.get("bot_capability"),
                    "bot_runtime_proof": envelope.content.get("bot_runtime_proof"),
                }
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
                bot_capability = await _authorize_bot_dm_open_capability(
                    session,
                    redis,
                    snowflake,
                    settings,
                    open_request,
                    users,
                    relay_domain=envelope.origin,
                    pair_key=dm_pair_key(*handles),
                    authority_domain=settings.domain,
                    runtime_preapplied=bot_dm_runtime_preapplied,
                    commit_runtime=False,
                )
                if bot_capability is None:
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
                if bot_capability is not None:
                    await apply_bot_dm_capability(
                        session,
                        snowflake,
                        bot_capability[0],
                        bot_capability[1],
                        conversation=conversation,
                        runtime_admitted=True,
                        admit_fenced_projection=True,
                    )
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
                        **(
                            {"bot_capability": bot_capability[0].model_dump(mode="json")}
                            if bot_capability is not None
                            else {}
                        ),
                        **(
                            {
                                "bot_runtime_proof": open_request.bot_runtime_proof.model_dump(
                                    mode="json"
                                )
                            }
                            if open_request.bot_runtime_proof is not None
                            else {}
                        ),
                    },
                )
                await queue_event(session, settings, envelope.origin, created)
            delivery_wakes.add(envelope.origin)
        elif envelope.type == "dm.group.call.create":
            replicated_group_call = await _apply_group_call_event(session, envelope)
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
            validate_dm_forward_age_context(envelope.content, raw_message)
            conversation_ref = (
                database_snowflake(raw_message.get("channel_id"), "DM channel id"),
                normalize_domain(str(raw_message.get("channel_domain", ""))),
            )
            message_conversation = await session.get(DMConversation, conversation_ref)
            if (authority_attested_direct_control or authority_attested_pin_notice) and (
                message_conversation is None
                or message_conversation.type != "direct"
                or message_conversation.authority_domain != envelope.origin
                or message_conversation.origin_domain != envelope.origin
            ):
                raise ValueError(
                    "authority-attested DM message is not bound to its direct conversation"
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
            message_channel = await session.get(Channel, conversation_ref)
            if message_channel is None:
                raise FederationResyncRetry
            await validate_dm_forward_source_proof(
                session,
                settings,
                envelope.content,
                raw_message,
                message_channel,
                validation_time=datetime.fromtimestamp(envelope.ts / 1000, tz=UTC),
            )
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
                "authority_domain": envelope.origin,
            }
        elif envelope.type in ANNOUNCEMENT_FOLLOW_LIFECYCLE_EVENTS:
            delivery_wakes.update(
                await apply_announcement_follow_lifecycle_event(
                    session,
                    redis,
                    snowflake,
                    settings,
                    event_type=envelope.type,
                    event_origin=envelope.origin,
                    event_timestamp_ms=envelope.ts,
                    event_content=envelope.content,
                    event_context=envelope.context,
                    raw_envelope=serialized_envelope,
                )
            )
        elif envelope.type == "announcement.crosspost.sync":
            content = envelope.content
            if set(content) != {
                "follow_id",
                "generation",
                "source_channel_ref",
                "source_message_ref",
                "source_deleted",
                "source_author",
                "message",
            }:
                raise ValueError("announcement crosspost sync content is malformed")
            follow_id = database_snowflake(content.get("follow_id"), "announcement follow id")
            generation = database_snowflake(
                content.get("generation"), "announcement follow generation"
            )
            source_channel_ref = EntityRef(str(content.get("source_channel_ref", ""))).resolve(
                settings.domain
            )
            source_message_ref = EntityRef(str(content.get("source_message_ref", ""))).resolve(
                settings.domain
            )
            source_deleted = content.get("source_deleted")
            if (
                not isinstance(source_deleted, bool)
                or source_channel_ref[1] != envelope.origin
                or source_message_ref[1] != envelope.origin
                or envelope.context.get("guild_domain") != envelope.origin
                or envelope.context.get("channel_id") != str(source_channel_ref[0])
                or envelope.context.get("channel_domain") != source_channel_ref[1]
            ):
                raise ValueError("announcement crosspost sync authority is invalid")
            await lock_announcement_mutation(session)
            unlocked_follow = await session.get(
                FederatedChannelFollow,
                federated_follow_key(follow_id, settings.domain, "target"),
            )
            if (
                unlocked_follow is None
                or unlocked_follow.source_authority_domain != envelope.origin
                or (
                    unlocked_follow.source_channel_id,
                    unlocked_follow.source_channel_domain,
                )
                != source_channel_ref
                or unlocked_follow.target_authority_domain != settings.domain
            ):
                raise ValueError("announcement crosspost sync receipt is stale")
            unlocked_receipt = await session.get(
                FederatedMessageCrosspost,
                federated_crosspost_key(
                    source_message_ref[0],
                    source_message_ref[1],
                    unlocked_follow.id,
                    unlocked_follow.target_authority_domain,
                    "target",
                ),
            )
            if (
                unlocked_receipt is None
                or unlocked_receipt.generation != generation
                or unlocked_receipt.delivery_status != "delivered"
                or unlocked_receipt.destination_message_id is None
                or unlocked_receipt.destination_message_domain is None
            ):
                raise ValueError("announcement crosspost sync has no published copy")
            unlocked_destination = await session.get(
                Message,
                (
                    unlocked_receipt.destination_message_id,
                    unlocked_receipt.destination_message_domain,
                ),
            )
            if (
                unlocked_destination is None
                or unlocked_destination.deleted_at is not None
                or not int(unlocked_destination.flags or 0) & MESSAGE_FLAG_IS_CROSSPOST
                or (
                    unlocked_destination.forwarded_message_id,
                    unlocked_destination.forwarded_message_domain,
                )
                != source_message_ref
                or (
                    unlocked_destination.forwarded_channel_id,
                    unlocked_destination.forwarded_channel_domain,
                )
                != source_channel_ref
            ):
                raise ValueError("announcement crosspost destination is invalid")
            unlocked_target = await session.get(
                Channel,
                (
                    unlocked_destination.channel_id,
                    unlocked_destination.channel_domain,
                ),
            )
            if (
                unlocked_target is None
                or unlocked_target.guild_id is None
                or unlocked_target.origin_domain != settings.domain
            ):
                raise ValueError("announcement crosspost target is invalid")
            target_guild = await session.get(
                Guild,
                (unlocked_target.guild_id, unlocked_target.guild_domain),
                with_for_update=True,
                populate_existing=True,
            )
            if (
                target_guild is None
                or target_guild.origin_domain != settings.domain
                or target_guild.unavailable
            ):
                raise ValueError("announcement crosspost target guild is invalid")
            target = await session.get(
                Channel,
                (unlocked_target.id, unlocked_target.origin_domain),
                with_for_update=True,
                populate_existing=True,
            )
            follow = await session.get(
                FederatedChannelFollow,
                federated_follow_key(follow_id, settings.domain, "target"),
                with_for_update=True,
                populate_existing=True,
            )
            receipt = await session.get(
                FederatedMessageCrosspost,
                federated_crosspost_key(
                    source_message_ref[0],
                    source_message_ref[1],
                    unlocked_follow.id,
                    unlocked_follow.target_authority_domain,
                    "target",
                ),
                with_for_update=True,
                populate_existing=True,
            )
            crosspost_destination = await session.get(
                Message,
                (
                    unlocked_receipt.destination_message_id,
                    unlocked_receipt.destination_message_domain,
                ),
                with_for_update=True,
                populate_existing=True,
            )
            if follow is None:
                raise ValueError("announcement crosspost sync receipt changed while locking")
            source_grant = stored_announcement_follow_projection(follow)
            if (
                target is None
                or target.unavailable
                or target.type != 0
                or target.origin_domain != settings.domain
                or (target.guild_id, target.guild_domain)
                != (target_guild.id, target_guild.origin_domain)
                or not source_deleted
                and (target.encryption_mode == "e2ee" or target.e2ee_required)
                or follow.source_authority_domain != envelope.origin
                or (follow.source_channel_id, follow.source_channel_domain) != source_channel_ref
                or follow.target_authority_domain != settings.domain
                or (follow.target_channel_id, follow.target_channel_domain)
                != (target.id, target.origin_domain)
                or receipt is None
                or receipt.generation != generation
                or receipt.delivery_status != "delivered"
                or receipt.destination_message_id is None
                or receipt.destination_message_domain is None
                or crosspost_destination is None
                or crosspost_destination.deleted_at is not None
                or not int(crosspost_destination.flags or 0) & MESSAGE_FLAG_IS_CROSSPOST
                or (
                    receipt.destination_message_id,
                    receipt.destination_message_domain,
                )
                != (crosspost_destination.id, crosspost_destination.origin_domain)
                or (
                    crosspost_destination.forwarded_message_id,
                    crosspost_destination.forwarded_message_domain,
                )
                != source_message_ref
                or (
                    crosspost_destination.forwarded_channel_id,
                    crosspost_destination.forwarded_channel_domain,
                )
                != source_channel_ref
                or (crosspost_destination.channel_id, crosspost_destination.channel_domain)
                != (target.id, target.origin_domain)
                or set(envelope.context)
                != {"guild_id", "guild_domain", "channel_id", "channel_domain"}
                or envelope.context.get("guild_id") != str(source_grant.source_guild_ref[0])
                or envelope.context.get("guild_domain") != source_grant.source_guild_ref[1]
            ):
                raise ValueError("announcement crosspost sync receipt changed while locking")
            author = await session.get(
                User,
                (crosspost_destination.author_id, crosspost_destination.author_domain),
            )
            if author is None:
                raise ValueError("announcement crosspost author is unavailable")
            source_author_profile = validate_announcement_sync_author_profile(
                content.get("source_author"),
                author_ref=(author.id, author.origin_domain),
                source_deleted=source_deleted,
            )
            if source_author_profile is not None and not source_deleted:
                author = await resolve_delegated_profile(
                    session,
                    settings,
                    source_author_profile,
                    authority_origin=envelope.origin,
                )
            changed_at = datetime.fromtimestamp(envelope.ts / 1000, UTC)
            if (
                crosspost_destination.edited_at is not None
                and changed_at < crosspost_destination.edited_at
            ):
                raise ValueError("announcement crosspost sync regressed")
            validated_source: ValidatedAnnouncementSourceProjection | None = None
            if source_deleted:
                source_shadow = Message(
                    id=source_message_ref[0],
                    origin_domain=source_message_ref[1],
                    channel_id=source_channel_ref[0],
                    channel_domain=source_channel_ref[1],
                    author_id=author.id,
                    author_domain=author.origin_domain,
                    content=None,
                    e2ee=None,
                    embeds=[],
                    components=[],
                    sticker_items=[],
                    application_id=None,
                    application_domain=None,
                    view_version=0,
                    message_type=0,
                    flags=0,
                    webhook_id=None,
                    webhook_domain=None,
                    webhook_name=None,
                    webhook_avatar_hash=None,
                    webhook_avatar_url=None,
                )
                raw_attachments: list[dict[str, object]] = []
            else:
                raw_message = content.get("message")
                if not isinstance(raw_message, dict):
                    raise ValueError("announcement crosspost sync message is invalid")
                validated_source = validate_announcement_source_projection(
                    raw_message,
                    source_message_ref=source_message_ref,
                    source_channel_ref=source_channel_ref,
                    author_ref=(author.id, author.origin_domain),
                )
                source_shadow = validated_source.message
                raw_attachments = validated_source.attachments
            apply_announcement_copy_projection(
                crosspost_destination,
                source_shadow,
                changed_at=changed_at,
                source_deleted=source_deleted,
            )
            replicated_attachments = await replicate_message_attachments(
                session,
                settings,
                crosspost_destination,
                author,
                raw_attachments,
                allowed_attachment_origins={author.origin_domain, envelope.origin},
            )
            incoming_attachment_refs = {
                (item.id, item.origin_domain) for item in replicated_attachments
            }
            stored_attachments = list(
                await session.scalars(
                    select(Attachment)
                    .where(
                        Attachment.message_id == crosspost_destination.id,
                        Attachment.message_domain == crosspost_destination.origin_domain,
                        Attachment.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            )
            for stored_attachment in stored_attachments:
                if (
                    stored_attachment.id,
                    stored_attachment.origin_domain,
                ) not in incoming_attachment_refs:
                    stored_attachment.deleted_at = changed_at
                    if stored_attachment.origin_domain == settings.domain:
                        local_media_purge_refs.append(
                            (stored_attachment.id, stored_attachment.origin_domain)
                        )
                    else:
                        remote_media_purge_refs.append(
                            (stored_attachment.origin_domain, stored_attachment.id)
                        )
            destination_view = await session.get(
                MessageView,
                (crosspost_destination.id, crosspost_destination.origin_domain),
                with_for_update=True,
            )
            destination_view = await sync_target_announcement_copy_view(
                session,
                target_guild,
                crosspost_destination,
                destination_view,
                (
                    validated_announcement_copy_view_projection(validated_source)
                    if validated_source is not None
                    else None
                ),
            )
            rendered = message_payload(
                crosspost_destination,
                author,
                replicated_attachments,
                view=destination_view,
            )
            crosspost_signer = await guild_authority_owner(
                session,
                settings,
                target_guild,
            )
            await queue_guild_mutation(
                session,
                settings,
                target_guild,
                crosspost_signer,
                "guild.message.update",
                {"message": rendered, "announcement_copy_updated": True},
                channel=target,
            )
            announcement_sync_guild = target_guild
            announcement_sync_payload = rendered
        elif envelope.type in {"guild.message.create", "guild.message.committed"}:
            raw_message = envelope.content["message"]
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
            raw_flags = raw_message.get("flags")
            crosspost_flag = bool(
                isinstance(raw_flags, int)
                and not isinstance(raw_flags, bool)
                and raw_flags & MESSAGE_FLAG_IS_CROSSPOST
            )
            valid_crosspost_actor = authority_attested_guild_crosspost_actor(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=envelope.origin,
                expected_guild_id=replicated_guild.id,
                expected_owner=(
                    replicated_guild.owner_id,
                    replicated_guild.owner_domain,
                ),
                actor=(
                    database_snowflake(envelope.actor.id, "guild crosspost actor id"),
                    envelope.actor.domain,
                ),
            )
            if crosspost_flag and not valid_crosspost_actor:
                raise ValueError("announcement copy authority binding is invalid")
            if envelope.type == "guild.message.committed" and (
                database_snowflake(envelope.actor.id, "guild commit actor id"),
                envelope.actor.domain,
            ) != (replicated_guild.owner_id, replicated_guild.owner_domain):
                raise ValueError("guild message was not signed for the guild owner")
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
                raw_role_refs = envelope.content.get("role_ids", [])
                if not isinstance(raw_role_refs, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    for item in raw_role_refs
                ):
                    raise RuntimeError("validated guild member roles lost their wire projection")
                replicated_guild_member_role_ids = [item["id"] for item in raw_role_refs]
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
                    replicated_tracker_dispatch_queued = (
                        envelope.type == "guild.tracker.board.invalidate"
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
            if queued_proxy_request_value is None:
                raise RuntimeError("queued proxy request validation state disappeared")
            proxy_request = queued_proxy_request_value
            actor = await upsert_remote_user(session, settings, proxy_request.actor)
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
                    int(proxy_request.channel_id),
                    guild.origin_domain,
                ),
            )
            if (
                loaded_proxy_channel is None
                or loaded_proxy_channel.guild_id != guild.id
                or not is_message_capable_channel_type(
                    loaded_proxy_channel.type,
                    guild_channel=True,
                )
            ):
                raise ValueError("proxy channel is not in the guild")
            channel = loaded_proxy_channel
            raw_attachments = proxy_request.attachments
            require_message_encryption_policy(
                channel,
                content=proxy_request.content,
                e2ee=proxy_request.e2ee,
                attachment_count=len(raw_attachments),
            )
            encrypted_rich = isinstance(proxy_request.e2ee, dict) and (
                "rich_payload_digest" in proxy_request.e2ee
            )
            encrypted_forward_routing = bool(
                encrypted_rich
                and isinstance(proxy_request.e2ee, dict)
                and proxy_request.e2ee.get("forward_snapshot_digest") is not None
            )
            encrypted_contract, encrypted_controls, encrypted_poll = encrypted_rich_routing(
                proxy_request.e2ee
            )
            needed = Permission.VIEW_CHANNEL | (
                Permission.SEND_MESSAGES_IN_THREADS
                if channel.type in {10, 11, 12}
                else Permission.SEND_MESSAGES
            )
            if raw_attachments:
                needed |= Permission.ATTACH_FILES
            if proxy_request.voice_message:
                needed |= Permission.SEND_VOICE_MESSAGES
            if proxy_request.tts:
                needed |= Permission.SEND_TTS_MESSAGES
            if proxy_request.poll is not None or encrypted_poll is not None:
                needed |= Permission.SEND_POLLS
            actor_permissions = await require_permissions(
                session,
                redis,
                guild,
                actor,
                needed,
                channel=channel,
            )
            application_ref = await validated_proxy_application(
                session,
                guild,
                actor,
                proxy_request.application_id,
            )
            if encrypted_forward_routing:
                if proxy_request.expression_authorizations:
                    raise ValueError("forward expression authorizations are not valid")
            else:
                try:
                    expression_tokens = expression_custom_emoji_tokens(
                        content=proxy_request.content,
                        components=proxy_request.components,
                        poll=proxy_request.poll,
                        e2ee=proxy_request.e2ee,
                        default_domain=guild.origin_domain,
                    )
                    attested_tokens, attested_items = await validate_expression_authorization_map(
                        session,
                        redis,
                        settings,
                        proxy_request.expression_authorizations,
                        requester_ref=f"{actor.id}@{actor.origin_domain}",
                        requester_type=cast(Literal["human", "bot"], actor.account_type),
                        application_ref=(
                            f"{application_ref[0]}@{application_ref[1]}"
                            if application_ref is not None
                            else None
                        ),
                        target_guild_ref=f"{guild.id}@{guild.origin_domain}",
                        target_channel_ref=f"{channel.id}@{channel.origin_domain}",
                        target_message_ref=None,
                        operation="message.create",
                        operation_id=proxy_request.client_nonce,
                        emoji_tokens=expression_tokens,
                        sticker_items=proxy_request.sticker_items,
                    )
                    await validate_attested_expression_target(
                        session,
                        actor,
                        guild,
                        actor_permissions,
                        attested_tokens,
                        attested_items,
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
                    ) from exc
            require_voice_message_attachments(proxy_request.voice_message, raw_attachments)
            await require_voice_message_guild_capacity(
                session,
                guild,
                voice_message=proxy_request.voice_message,
            )
            if (
                channel.type in {10, 11, 12}
                and channel.locked
                and not actor_permissions & Permission.MANAGE_THREADS
            ):
                raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})
            await require_proxy_bot_e2ee_participation(
                session,
                guild,
                channel,
                actor,
                application_ref,
                proxy_request.e2ee,
            )
            encrypted_forward = encrypted_forward_routing
            transported_forward = proxy_request.forward_snapshot is not None or encrypted_forward
            forwarded_message = await validated_proxy_forward(
                session,
                redis,
                settings,
                guild,
                channel,
                actor,
                (None if transported_forward else proxy_request.forwarded_message_id),
            )
            forward_proof: dict[str, object] | None = None
            if transported_forward:
                if (
                    proxy_request.forward_source_nsfw is None
                    or proxy_request.forward_source_proof is None
                    or proxy_request.forwarded_message_id is None
                    or proxy_request.forwarded_channel_id is None
                ):
                    raise ValueError("proxy forward source proof is missing")
                forwarded_ref = proxy_request.forwarded_message_id.resolve(settings.domain)
                forwarded_channel_ref = proxy_request.forwarded_channel_id.resolve(settings.domain)
                proof_device_id = (
                    cast(str, proxy_request.e2ee.get("sender_device_id"))
                    if actor.account_type == "bot"
                    and isinstance(proxy_request.e2ee, dict)
                    and isinstance(proxy_request.e2ee.get("sender_device_id"), str)
                    else None
                )
                forward_proof = await validate_signed_forward_source_proof(
                    session,
                    settings,
                    proxy_request.forward_source_proof,
                    requester=actor,
                    source_message_ref=forwarded_ref,
                    source_channel_ref=forwarded_channel_ref,
                    destination_channel=channel,
                    nonce=proxy_request.client_nonce,
                    application_ref=application_ref,
                    e2ee_device_id=proof_device_id,
                    validation_time=datetime.fromtimestamp(envelope.ts / 1000, tz=UTC),
                )
                if forward_proof["source_nsfw"] is not proxy_request.forward_source_nsfw:
                    raise ValueError("proxy forward source proof is inconsistent")
                if (
                    forward_proof["source_encryption_mode"] == "plaintext"
                    and proxy_request.forward_snapshot is not None
                    and proxy_request.forward_snapshot != forward_proof["source_snapshot"]
                ):
                    raise ValueError("proxy forward source proof is inconsistent")
                if (
                    forward_proof["source_encryption_mode"] == "e2ee"
                    and proxy_request.forward_snapshot is not None
                ):
                    try:
                        require_disclosed_forward_snapshot_proof_binding(
                            proxy_request.forward_snapshot,
                            forward_proof,
                        )
                    except ValueError as exc:
                        raise ValueError("proxy forward source proof is inconsistent") from exc
                await validate_attested_forward_expressions(
                    session,
                    actor,
                    guild,
                    actor_permissions,
                    e2ee=proxy_request.e2ee,
                    routed_sticker_items=proxy_request.sticker_items,
                    forward_snapshot=proxy_request.forward_snapshot,
                    forward_proof=forward_proof,
                    trusted_external_domain=actor.origin_domain,
                )
                await require_attested_forward_age_context(
                    session,
                    channel,
                    proxy_request.forward_source_nsfw,
                )
                source_attachment_count = len(
                    cast(list[str], forward_proof["source_attachment_refs"])
                )
                if (
                    encrypted_forward or forward_proof["source_encryption_mode"] == "e2ee"
                ) and source_attachment_count != len(raw_attachments):
                    raise ValueError("proxy forward attachment count is inconsistent")
            if (
                proxy_request.forward_snapshot is not None
                and raw_attachments
                and not forward_snapshot_matches_attachments(
                    proxy_request.forward_snapshot,
                    raw_attachments,
                )
            ):
                raise ValueError("proxy forward attachment binding is invalid")
            nonce = proxy_request.client_nonce
            await lock_proxy_nonce(session, guild, actor, channel, nonce)
            proxy_content = proxy_request.content
            proxy_e2ee = proxy_request.e2ee
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
            if (
                proxy_content is None
                and proxy_e2ee is None
                and not raw_attachments
                and not proxy_request.embeds
                and not proxy_request.components
                and proxy_request.poll is None
                and proxy_request.forwarded_message_id is None
            ):
                raise ValueError(
                    "proxy write requires content, an attachment, rich content, or a forward"
                )
            raw_reference = proxy_request.referenced_message_id
            referenced_message: Message | None = None
            if raw_reference is not None:
                reference_id, reference_domain = raw_reference.resolve(settings.domain)
                referenced_message = await session.get(Message, (reference_id, reference_domain))
                if referenced_message is None or (
                    referenced_message.channel_id,
                    referenced_message.channel_domain,
                ) != (channel.id, channel.origin_domain):
                    raise ValueError("proxy write references a message outside the channel")
            mention_projection = await resolve_proxy_guild_mentions(
                session,
                redis,
                settings,
                guild,
                channel,
                actor,
                actor_permissions,
                proxy_request,
                referenced=referenced_message,
            )
            parsed_mention_refs = list(mention_projection.recipient_refs)
            mention_refs = list(mention_projection.recipient_payload)
            role_mention_recipient_refs = set(mention_projection.role_recipients)
            interaction_projection = await authoritative_proxy_interaction_projection(
                session,
                settings,
                guild,
                actor,
                application_ref,
                proxy_request,
                referenced_message,
            )
            forwarded_created_at = (
                datetime.fromisoformat(cast(str, forward_proof["source_created_at"]))
                if encrypted_forward and forward_proof is not None
                else None
            )
            forwarded_edited_at = (
                datetime.fromisoformat(cast(str, forward_proof["source_edited_at"]))
                if encrypted_forward
                and forward_proof is not None
                and forward_proof.get("source_edited_at") is not None
                else None
            )
            require_encrypted_rich_admission(
                proxy_e2ee,
                author=actor,
                attachments=raw_attachments,
                mention_refs=[(int(item["id"]), item["origin_domain"]) for item in mention_refs],
                sticker_items=proxy_request.sticker_items,
                referenced_message_ref=(
                    raw_reference.resolve(settings.domain) if raw_reference is not None else None
                ),
                application_ref=application_ref,
                installation_lineage=interaction_projection.transport_lineage,
                has_controls=bool(encrypted_controls),
                tts=proxy_request.tts,
                voice_message=proxy_request.voice_message,
                flags=proxy_request.flags,
                view_persistent=proxy_request.view_persistent,
                view_version=1 if encrypted_controls else 0,
                forwarded_message_ref=(
                    proxy_request.forwarded_message_id.resolve(settings.domain)
                    if proxy_request.forwarded_message_id is not None
                    else None
                ),
                forwarded_channel_ref=(
                    proxy_request.forwarded_channel_id.resolve(settings.domain)
                    if proxy_request.forwarded_channel_id is not None
                    else None
                ),
                forward_source_projection_digest=(
                    cast(str, forward_proof["source_projection_digest"])
                    if encrypted_forward and forward_proof is not None
                    else None
                ),
                forwarded_created_at=forwarded_created_at,
                forwarded_edited_at=forwarded_edited_at,
                forwarded_flags=(
                    cast(int, forward_proof["source_flags"])
                    if encrypted_forward and forward_proof is not None
                    else None
                ),
                forwarded_message_type=(
                    cast(int, forward_proof["source_message_type"])
                    if encrypted_forward and forward_proof is not None
                    else None
                ),
            )
            home_message = await session.scalar(
                select(Message).where(
                    Message.channel_id == channel.id,
                    Message.channel_domain == channel.origin_domain,
                    Message.author_id == actor.id,
                    Message.author_domain == actor.origin_domain,
                    Message.client_nonce == nonce,
                )
            )
            if home_message is not None and not await proxy_message_matches_request(
                session,
                home_message,
                proxy_request,
                application_ref=application_ref,
                installation_lineage=interaction_projection.installation_lineage,
                forwarded_message=forwarded_message,
                mentions=mention_projection,
            ):
                raise ValueError("proxy nonce replay changed immutable message fields")
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
                home_automod_post_commit = await evaluate_automod_message(
                    session,
                    redis,
                    settings,
                    snowflake,
                    guild,
                    channel,
                    actor,
                    message_automod_text(
                        proxy_content,
                        poll=proxy_request.poll,
                        components=proxy_request.components,
                    ),
                    mention_count=len(mention_refs),
                    actor_permissions=actor_permissions,
                    commit_on_block=False,
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
                    tts=proxy_request.tts,
                    embeds=[
                        item.model_dump(mode="json", exclude_none=True)
                        for item in proxy_request.embeds
                    ],
                    components=[
                        item.model_dump(mode="json", exclude_none=True)
                        for item in proxy_request.components
                    ],
                    sticker_items=proxy_request.sticker_items,
                    application_id=(application_ref[0] if application_ref is not None else None),
                    application_domain=(
                        application_ref[1] if application_ref is not None else None
                    ),
                    view_version=(
                        1
                        if (proxy_request.components or encrypted_controls)
                        and application_ref is not None
                        else 0
                    ),
                    forwarded_message_id=(
                        proxy_request.forwarded_message_id.id
                        if proxy_request.forwarded_message_id is not None
                        else None
                    ),
                    forwarded_message_domain=(
                        proxy_request.forwarded_message_id.resolve(settings.domain)[1]
                        if proxy_request.forwarded_message_id is not None
                        else None
                    ),
                    forwarded_channel_id=(
                        proxy_request.forwarded_channel_id.id
                        if proxy_request.forwarded_channel_id is not None
                        else None
                    ),
                    forwarded_channel_domain=(
                        proxy_request.forwarded_channel_id.resolve(settings.domain)[1]
                        if proxy_request.forwarded_channel_id is not None
                        else None
                    ),
                    forward_snapshot=proxy_request.forward_snapshot,
                    encryption_policy_generation=channel.encryption_policy_generation,
                    encryption_epoch=channel.encryption_epoch,
                    client_nonce=nonce,
                    referenced_message_id=(
                        referenced_message.id if referenced_message is not None else None
                    ),
                    referenced_message_domain=(
                        referenced_message.origin_domain if referenced_message is not None else None
                    ),
                    message_type=interaction_projection.message_type,
                    interaction_metadata=interaction_projection.metadata,
                    mention_user_refs=mention_refs,
                    mention_role_refs=[
                        {"id": str(role_id), "origin_domain": role_domain}
                        for role_id, role_domain in mention_projection.role_refs
                    ],
                    mention_everyone=mention_projection.everyone,
                    flags=(0 if actor_permissions & Permission.EMBED_LINKS else 4)
                    | (
                        proxy_request.flags
                        & (MESSAGE_FLAG_SUPPRESS_EMBEDS | MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS)
                    )
                    | inferred_message_shape_flags(
                        voice_message=proxy_request.voice_message,
                        components_v2=uses_components_v2(proxy_request.components),
                    )
                    | (proxy_request.flags & MESSAGE_FLAG_IS_COMPONENTS_V2 if encrypted_rich else 0)
                    | (MESSAGE_FLAG_HAS_SNAPSHOT if transported_forward else 0),
                )
                session.add(home_message)
                await session.flush()
                if proxy_request.poll is not None:
                    session.add(
                        Poll(
                            message_id=home_message.id,
                            message_domain=home_message.origin_domain,
                            question=proxy_request.poll.question.model_dump(
                                mode="json", exclude_none=True
                            ),
                            allow_multiselect=proxy_request.poll.allow_multiselect,
                            layout_type=proxy_request.poll.layout_type,
                            expires_at=datetime.now(UTC)
                            + timedelta(hours=proxy_request.poll.duration),
                        )
                    )
                    for answer_id, answer in enumerate(proxy_request.poll.answers, start=1):
                        session.add(
                            PollAnswer(
                                message_id=home_message.id,
                                message_domain=home_message.origin_domain,
                                answer_id=answer_id,
                                text=answer.poll_media.text,
                                emoji=(
                                    answer.poll_media.emoji.model_dump(
                                        mode="json", exclude_none=True
                                    )
                                    if answer.poll_media.emoji is not None
                                    else None
                                ),
                            )
                        )
                elif encrypted_poll is not None:
                    add_encrypted_poll_rows(session, home_message, encrypted_poll)
                if (proxy_request.components or encrypted_controls) and application_ref is not None:
                    if interaction_projection.installation_lineage is None:
                        raise RuntimeError("interactive proxy message lost authority lineage")
                    installation_lineage = interaction_projection.installation_lineage
                    session.add(
                        MessageView(
                            message_id=home_message.id,
                            message_domain=home_message.origin_domain,
                            application_id=application_ref[0],
                            application_domain=application_ref[1],
                            integration_type=installation_lineage[0],
                            installation_id=installation_lineage[1],
                            installation_domain=installation_lineage[2],
                            installation_revision=installation_lineage[3],
                            version=1,
                            persistent=proxy_request.view_persistent,
                            expires_at=(
                                None
                                if proxy_request.view_persistent
                                else datetime.now(UTC)
                                + timedelta(
                                    seconds=(
                                        cast(int, encrypted_contract["view_timeout_seconds"])
                                        if encrypted_contract is not None
                                        else proxy_request.view_timeout_seconds or 900
                                    )
                                )
                            ),
                        )
                    )
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
                        role_mention_recipient_refs,
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
                if not home_message_created:
                    raise ProxyNonceStateConflict(
                        "existing proxy message has no retained commit event"
                    )
                seq = await assign_guild_sequence(session, guild)
                proxy_receipt = proxy_request_fingerprint_receipt(
                    proxy_request,
                    guild.origin_domain,
                )
                bind_proxy_commit_receipt(home_message, proxy_receipt, seq)
                owner = await guild_authority_owner(session, settings, guild)
                committed = await build_guild_authority_envelope(
                    session,
                    settings,
                    guild,
                    "guild.message.committed",
                    owner,
                    {
                        "message": await render_message_payload(
                            session,
                            home_message,
                            actor,
                            viewer=actor,
                            include_forward_source=True,
                        ),
                        "author": profile_from_user(actor),
                        "thread_starter": False,
                        "proxy_request_fingerprint": proxy_receipt.wire(),
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
        if replicated_group_call is not None:
            replicated_group_call_identities = set(
                cast(list[str], replicated_group_call["participants"])
            )
            await _ensure_replicated_group_call(
                redis,
                settings,
                replicated_group_call,
                replicated_group_call_identities,
            )
        inbox.status = "processed"
        inbox.result_code = None
        inbox.processed_at = datetime.now(UTC)
        await event_work.commit()
        replicated_guild_ref: tuple[int, str] | None = None
        replicated_guild_message_ref: tuple[int, str] | None = None
        replicated_guild_message_payload: dict[str, object] | None = None
        replicated_thread_projection: ThreadDispatchProjection | None = None
        if replicated_guild_message is not None and replicated_guild is not None:
            replicated_guild_ref = (replicated_guild.id, replicated_guild.origin_domain)
            replicated_guild_message_ref = (
                replicated_guild_message.id,
                replicated_guild_message.origin_domain,
            )
            await session.refresh(replicated_guild_message)
            replicated_guild_message_payload = await render_message_payload(
                session,
                replicated_guild_message,
            )
            if replicated_thread_message is not None:
                replicated_thread_projection = await materialize_thread_dispatch(
                    session,
                    replicated_thread_message,
                )

        home_thread_projection = (
            await materialize_thread_dispatch(
                session,
                home_thread,
                home_thread_members_added,
            )
            if home_thread is not None
            else None
        )
        home_message_ref: tuple[int, str] | None = None
        home_message_guild_ref: tuple[int, str] | None = None
        home_message_payload: dict[str, object] | None = None
        if home_message is not None and home_message_created:
            await session.refresh(home_message)
            for attachment in home_message_attachments:
                await session.refresh(attachment)
            home_message_ref = (home_message.id, home_message.origin_domain)
            home_message_guild_ref = (
                int(envelope.context["guild_id"]),
                home_message.channel_domain,
            )
            home_message_payload = message_payload(
                home_message,
                await session.get(User, (home_message.author_id, home_message.author_domain)),
                home_message_attachments,
            )
        if inserted_event is not None:
            peer.federation_inbox_event_bytes += envelope_bytes
            global_ledger.federation_inbox_event_bytes += envelope_bytes
        await commit_inbox_state()
        durably_committed = True
        await home_automod_post_commit.publish(redis)
        if replicated_tracker_dispatch_queued:
            await wake_tracker_dispatch_outbox()
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
        if interaction_response_dispatch is not None:
            interaction_event_name, interaction_event_payload = interaction_response_dispatch
            if not interaction_response_dispatch_expired(
                {"t": interaction_event_name, "d": interaction_event_payload}
            ):
                await publish_ephemeral(
                    redis,
                    user_topic(
                        settings.domain,
                        database_snowflake(envelope.actor.id, "user id"),
                    ),
                    interaction_event_name,
                    interaction_event_payload,
                )
            await wake_interaction_dispatch_outbox()
        if replicated_message is not None:
            await publish_replicated_dm_message(session, redis, settings, replicated_message)
            await enqueue_best_effort(
                mentions_fanout,
                replicated_message.id,
                replicated_message.origin_domain,
            )
        if replicated_dm_poll_mutation is not None:
            local_poll_participants = list(
                await session.execute(
                    select(DMParticipant.user_id, DMParticipant.user_domain).where(
                        DMParticipant.conversation_id == replicated_dm_poll_mutation.channel.id,
                        DMParticipant.conversation_domain
                        == replicated_dm_poll_mutation.channel.origin_domain,
                        DMParticipant.user_domain == settings.domain,
                    )
                )
            )
            for event_name, event_payload in replicated_dm_poll_mutation.vote_events:
                for user_id, user_domain in local_poll_participants:
                    await publish_dispatch(
                        redis,
                        user_topic(user_domain, user_id),
                        event_name,
                        event_payload,
                    )
            if replicated_dm_poll_mutation.finalized:
                for user_id, user_domain in local_poll_participants:
                    viewer = await session.get(User, (user_id, user_domain))
                    if viewer is not None:
                        await publish_dispatch(
                            redis,
                            user_topic(user_domain, user_id),
                            "MESSAGE_UPDATE",
                            await render_message_payload(
                                session,
                                replicated_dm_poll_mutation.message,
                                viewer=viewer,
                            ),
                        )
        if replicated_dm_message_mutation is not None:
            local_mutation_participants = list(
                await session.execute(
                    select(DMParticipant.user_id, DMParticipant.user_domain).where(
                        DMParticipant.conversation_id == replicated_dm_message_mutation.channel.id,
                        DMParticipant.conversation_domain
                        == replicated_dm_message_mutation.channel.origin_domain,
                        DMParticipant.user_domain == settings.domain,
                    )
                )
            )
            if replicated_dm_message_mutation.render_message_update:
                for user_id, user_domain in local_mutation_participants:
                    viewer = await session.get(User, (user_id, user_domain))
                    if viewer is not None:
                        await publish_dispatch(
                            redis,
                            user_topic(user_domain, user_id),
                            "MESSAGE_UPDATE",
                            await render_message_payload(
                                session,
                                replicated_dm_message_mutation.message,
                                viewer=viewer,
                            ),
                        )
            for event_name, event_payload in replicated_dm_message_mutation.dispatches:
                for user_id, user_domain in local_mutation_participants:
                    await publish_dispatch(
                        redis,
                        user_topic(user_domain, user_id),
                        event_name,
                        event_payload,
                    )
        if replicated_group_call is not None and replicated_group_call_identities is not None:
            local_call_identities = sorted(
                identity
                for identity in replicated_group_call_identities
                if parse_participant_identity(identity)[1] == settings.domain
            )
            await notify_call(
                session,
                redis,
                local_call_identities,
                "CALL_CREATE",
                replicated_group_call,
                settings,
            )
            await notify_call(
                session,
                redis,
                [
                    identity
                    for identity in local_call_identities
                    if identity != str(replicated_group_call["caller"])
                ],
                "CALL_RING",
                replicated_group_call,
                settings,
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
        if (
            replicated_guild_message_payload is not None
            and replicated_guild_message_ref is not None
            and replicated_guild_ref is not None
        ):
            if replicated_thread_projection is not None:
                await publish_dispatch(
                    redis,
                    guild_topic(
                        replicated_thread_projection.guild_ref[1],
                        replicated_thread_projection.guild_ref[0],
                    ),
                    "THREAD_UPDATE",
                    replicated_thread_projection.channel,
                )
            await publish_dispatch(
                redis,
                guild_topic(replicated_guild_ref[1], replicated_guild_ref[0]),
                "MESSAGE_CREATE",
                replicated_guild_message_payload,
            )
            await enqueue_best_effort(
                mentions_fanout,
                *replicated_guild_message_ref,
            )
        if announcement_sync_guild is not None and announcement_sync_payload is not None:
            await wake_queued_guild_federation(announcement_sync_guild)
            await publish_dispatch(
                redis,
                guild_topic(
                    announcement_sync_guild.origin_domain,
                    announcement_sync_guild.id,
                ),
                "MESSAGE_UPDATE",
                announcement_sync_payload,
            )
        if replicated_guild_member is not None and replicated_guild is not None:
            await publish_dispatch(
                redis,
                guild_topic(replicated_guild.origin_domain, replicated_guild.id),
                "GUILD_MEMBER_ADD",
                {
                    "guild_id": str(replicated_guild.id),
                    "user": user_payload(replicated_guild_member),
                    "role_ids": replicated_guild_member_role_ids,
                },
            )
        if authoritative_leave_guild is not None and authoritative_leave_target is not None:
            await wake_tracker_membership_cleanup(authoritative_leave_guild)
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
            if dispatch_type.startswith("GUILD_EMOJI_"):
                await publish_guild_emojis_update(session, redis, replicated_guild)
            elif dispatch_type.startswith("GUILD_STICKER_"):
                await publish_guild_stickers_update(session, redis, replicated_guild)
        if profile_member_dispatch is not None:
            profile_guild, profile_payload = profile_member_dispatch
            await publish_dispatch(
                redis,
                guild_topic(profile_guild.origin_domain, profile_guild.id),
                "GUILD_MEMBER_UPDATE",
                profile_payload,
            )
        if authoritative_profile_relay_guild is not None:
            await wake_queued_guild_federation(authoritative_profile_relay_guild)
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
        if home_thread is not None and home_thread_projection is not None:
            thread_topic = guild_topic(
                home_thread_projection.guild_ref[1],
                home_thread_projection.guild_ref[0],
            )
            if home_thread_updated:
                await publish_dispatch(
                    redis,
                    thread_topic,
                    "THREAD_UPDATE",
                    home_thread_projection.channel,
                )
            if home_thread_was_unarchived:
                home_thread_guild = await session.get(
                    Guild,
                    home_thread_projection.guild_ref,
                )
                if home_thread_guild is None:
                    raise RuntimeError("thread guild disappeared after unarchive")
                await publish_current_thread_member_updates(
                    session,
                    redis,
                    home_thread_guild,
                    home_thread,
                )
            if home_thread_projection.added_members:
                for (
                    target_ref,
                    rendered_member,
                    _rich_member,
                ) in home_thread_projection.added_members:
                    await publish_dispatch(
                        redis,
                        thread_topic,
                        "THREAD_CREATE",
                        home_thread_projection.channel | {"member": rendered_member},
                        audience_user_refs=[target_ref],
                    )
                    await publish_dispatch(
                        redis,
                        thread_topic,
                        "THREAD_MEMBER_UPDATE",
                        rendered_member,
                        audience_user_refs=[target_ref],
                    )
                if home_thread_projection.members_update is None:
                    raise RuntimeError("thread member projection is incomplete")
                await publish_dispatch(
                    redis,
                    thread_topic,
                    "THREAD_MEMBERS_UPDATE",
                    home_thread_projection.members_update,
                )
        if (
            home_message_payload is not None
            and home_message_ref is not None
            and home_message_guild_ref is not None
        ):
            await publish_dispatch(
                redis,
                guild_topic(home_message_guild_ref[1], home_message_guild_ref[0]),
                "MESSAGE_CREATE",
                home_message_payload,
            )
            await enqueue_best_effort(mentions_fanout, *home_message_ref)
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
        if (
            isinstance(exc, AutoModMessageBlocked)
            and envelope.type == "guild.proxy.message.create"
            and event_work.is_active
        ):
            guild = await home_guild(
                session,
                settings,
                database_snowflake(envelope.context.get("guild_id"), "guild id"),
            )
            owner = await guild_authority_owner(session, settings, guild)
            rejected = await build_guild_authority_envelope(
                session,
                settings,
                guild,
                "message.send_rejected",
                owner,
                {
                    "target": {"id": envelope.actor.id, "domain": envelope.actor.domain},
                    "channel_id": str(envelope.content.get("channel_id", "")),
                    "client_nonce": str(envelope.content.get("client_nonce", "")),
                    "code": "MESSAGE_BLOCKED_BY_AUTO_MOD",
                },
                context={"guild_id": str(guild.id), "guild_domain": guild.origin_domain},
            )
            await queue_event(session, settings, envelope.origin, rejected)
            inbox.status = "processed"
            inbox.result_code = None
            inbox.processed_at = datetime.now(UTC)
            await event_work.commit()
            if inserted_event is not None:
                peer.federation_inbox_event_bytes += envelope_bytes
                global_ledger.federation_inbox_event_bytes += envelope_bytes
            await commit_inbox_state()
            await exc.post_commit.publish(redis)
            await enqueue_best_effort(federation_deliver, envelope.origin)
            return InboxResult(event_id=envelope.event_id, status="processed")
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
        if _is_transient_event_infrastructure_error(exc):
            inbox = await session.get(FederationInbox, (envelope.origin, envelope.event_id))
            if inbox is not None:
                await session.delete(inbox)
                peer.federation_inbox_events = max(0, peer.federation_inbox_events - 1)
                global_ledger.federation_inbox_events = max(
                    0, global_ledger.federation_inbox_events - 1
                )
            await commit_inbox_state()
            log.warning(
                "federation_event_infrastructure_retry",
                origin=envelope.origin,
                event_id=envelope.event_id,
                event_type=envelope.type,
                error_type=type(exc).__name__,
            )
            return InboxResult(
                event_id=envelope.event_id,
                status="retry",
                code="KAED_FED_EVENT_RETRY",
            )
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
            await commit_inbox_state()
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
            await commit_inbox_state()
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
                await commit_inbox_state()
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
            await commit_inbox_state()
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
            await commit_inbox_state()
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
                await commit_inbox_state()
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
                        owner = await guild_authority_owner(session, settings, guild)
                        rejected = await build_guild_authority_envelope(
                            session,
                            settings,
                            guild,
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
            await commit_inbox_state()
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
            await commit_inbox_state()
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
            await commit_inbox_state()
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
                    owner = await guild_authority_owner(session, settings, guild)
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
                            "timeout_indefinite": rejection_detail.get("timeout_indefinite", False),
                        }
                        if code == "MEMBER_TIMED_OUT" and isinstance(rejection_detail, dict)
                        else {}
                    )
                    rejected = await build_guild_authority_envelope(
                        session,
                        settings,
                        guild,
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
        await commit_inbox_state()
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
            event_context=event.context,
        )
        if policy_code is not None:
            results.append(
                InboxResult(
                    event_id=event.event_id,
                    status="retry",
                    code=policy_code,
                ).model_dump()
            )
            break
        result = await process_event(session, redis, settings, principal, event, snowflake)
        results.append(result.model_dump())
        if result.status == "retry":
            break
    return {"results": results}


@router.get("/_kaede/v1/users/lookup")
async def federation_user_lookup(
    handle: str = Query(max_length=286),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "user-lookup",
        capacity=120,
        refill_per_minute=120,
    )
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
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "KAED_E2EE_TARGET_NOT_FOUND"})
    if channel.guild_id is not None:
        require_guild_federation_access(principal)
    target = await session.get(User, (int(payload.target_id), payload.target_domain))
    if target is None or not target.is_local:
        raise HTTPException(status_code=404, detail={"code": "KAED_E2EE_TARGET_NOT_FOUND"})
    claimant_ref = (int(payload.claimant_id), payload.claimant_domain)
    target_ref = (target.id, target.origin_domain)
    claimant = await session.get(User, claimant_ref)
    if claimant is None:
        raise HTTPException(status_code=404, detail={"code": "KAED_E2EE_TARGET_NOT_FOUND"})
    if channel.guild_id is not None:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is None:
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
    # A package claim consumes one-time key material, so it is a mutation even
    # though the response is read-shaped. Check authorization first to avoid
    # disclosing local moderation state to a non-participant, then apply the
    # receiving instance's suspension policy before any lease can be acquired.
    await require_remote_user_creation_allowed(session, claimant)
    excluded = (payload.excluded_device_id or "") if claimant_ref == target_ref else ""
    if target.account_type == "bot":
        if not payload.bot_device_ids or payload.excluded_device_id is not None:
            raise HTTPException(
                status_code=404,
                detail={"code": "KAED_E2EE_TARGET_NOT_FOUND"},
            )
        application = await session.scalar(
            select(BotApplication).where(
                BotApplication.bot_user_id == target.id,
                BotApplication.bot_user_domain == target.origin_domain,
                BotApplication.origin_domain == settings.domain,
                BotApplication.status == "active",
            )
        )
        if application is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "KAED_E2EE_TARGET_NOT_FOUND"},
            )
        packages = await claim_local_bot_room_key_packages(
            session,
            application=application,
            target=target,
            protocol_ids=payload.bot_device_ids,
            operation_id=payload.operation_id,
            operation_domain=payload.operation_domain,
            channel_ref=(channel.id, channel.origin_domain),
            claimant_ref=claimant_ref,
            max_devices=payload.max_devices,
        )
    else:
        if payload.bot_device_ids:
            raise HTTPException(
                status_code=404,
                detail={"code": "KAED_E2EE_TARGET_NOT_FOUND"},
            )
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


def require_channel_federation_access(
    principal: FederationPrincipal,
    channel: Channel,
) -> None:
    """Apply peer silence only when a shared route resolves to guild state."""

    if channel.guild_id is not None:
        require_guild_federation_access(principal)


async def federated_e2ee_actor(
    payload: E2EERoomProxyRequest | E2EERoomOperationStatusRequest,
    principal: FederationPrincipal,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    require_mutation_admission: bool = True,
) -> AuthenticatedUser:
    if payload.actor.origin_domain != principal.origin or payload.channel_domain != settings.domain:
        raise HTTPException(status_code=403, detail={"code": "KAED_E2EE_AUTHORITY_MISMATCH"})
    channel = await session.get(Channel, (int(payload.channel_id), payload.channel_domain))
    if channel is not None:
        require_channel_federation_access(principal, channel)
    actor = await upsert_remote_user(session, settings, payload.actor)
    if require_mutation_admission:
        access = await load_channel_access(
            session,
            settings,
            actor,
            EntityRef(f"{payload.channel_id}@{payload.channel_domain}"),
        )
        await require_room_policy_authority(
            session,
            redis,
            settings,
            access,
            actor,
        )
        await require_remote_user_creation_allowed(session, actor)
    return AuthenticatedUser(
        user=actor,
        grant=AccessGrant(actor.id, actor.origin_domain, f"federation:{principal.origin}"),
        access_token="",
        cookie_authenticated=False,
    )


async def enforce_e2ee_room_proxy_limit(redis: Redis, principal: FederationPrincipal) -> None:
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
    auth = await federated_e2ee_actor(payload, principal, session, redis, settings)
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
    auth = await federated_e2ee_actor(payload, principal, session, redis, settings)
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
    auth = await federated_e2ee_actor(payload, principal, session, redis, settings)
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
    auth = await federated_e2ee_actor(payload, principal, session, redis, settings)
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
    auth = await federated_e2ee_actor(
        payload,
        principal,
        session,
        redis,
        settings,
        require_mutation_admission=False,
    )
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

    await enforce_federation_route_rate_limit(
        redis, principal.origin, "presence", capacity=300, refill_per_minute=300
    )
    if payload.user_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    if not await receive_presence(
        session,
        redis,
        settings,
        payload,
        include_guilds=not principal.silenced,
    ):
        raise HTTPException(status_code=409, detail={"code": "KAED_PRESENCE_STALE_OR_UNKNOWN"})
    return Response(status_code=204)


@router.post("/_kaede/v1/typing/publish", status_code=204)
async def federation_typing_publish(
    payload: TypingPublishRequest,
    request: Request,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Admit a user-home typing request at the exact room authority."""

    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "typing-publish",
        capacity=600,
        refill_per_minute=600,
    )
    if (
        payload.user_domain != principal.origin
        or payload.actor.origin_domain != principal.origin
        or payload.channel_domain != settings.domain
        or not typing_projection_is_fresh(payload)
    ):
        raise HTTPException(status_code=409, detail={"code": "KAED_TYPING_SCOPE_INVALID"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    access = await load_channel_access(
        session,
        settings,
        actor,
        EntityRef(f"{payload.channel_id}@{payload.channel_domain}"),
    )
    require_channel_federation_access(principal, access.channel)
    await require_typing_access(session, redis, access, actor)
    await require_remote_user_creation_allowed(session, actor)
    projection = TypingProjection.model_validate(payload.model_dump(mode="json", exclude={"actor"}))
    sessionmaker = cast(async_sessionmaker[AsyncSession], request.app.state.sessionmaker)
    if not await publish_authoritative_typing(
        session,
        sessionmaker,
        redis,
        settings,
        access.channel,
        projection,
    ):
        raise HTTPException(status_code=409, detail={"code": "KAED_TYPING_STALE"})
    return Response(status_code=204)


@router.post("/_kaede/v1/typing/relay", status_code=204)
async def federation_typing_relay(
    payload: TypingRelayRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Accept one short-lived room-authority relay without durable queuing."""

    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "typing-relay",
        capacity=1_200,
        refill_per_minute=1_200,
    )
    if not typing_projection_is_fresh(payload):
        raise HTTPException(status_code=409, detail={"code": "KAED_TYPING_STALE"})
    try:
        channel, actor, audience_user_refs = await validate_typing_relay_scope(
            session,
            settings,
            payload,
            authority_domain=principal.origin,
        )
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail={"code": "KAED_TYPING_SCOPE_INVALID"},
        ) from None
    require_channel_federation_access(principal, channel)
    await require_remote_user_creation_allowed(session, actor)
    if not await accept_typing_generation(redis, payload, batch_index=payload.batch_index):
        raise HTTPException(status_code=409, detail={"code": "KAED_TYPING_STALE"})
    await publish_local_typing(
        session,
        redis,
        settings,
        channel,
        payload,
        audience_user_refs=audience_user_refs,
    )
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
            owner = await guild_authority_owner(session, settings, guild)
        replacement = await build_guild_authority_envelope(
            session,
            settings,
            guild,
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


@router.post("/_kaede/v1/channels/{source_channel_id}/forward-authorize")
async def federation_forward_source_authorize(
    source_channel_id: Snowflake,
    payload: ForwardSourceAuthorizeFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Issue one requester/destination/nonce-bound source-access proof."""

    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "forward-source-authorize",
        capacity=600,
        refill_per_minute=600,
    )
    if (
        payload.actor.origin_domain != principal.origin
        or payload.actor.account_type != "human"
        or payload.application_ref is not None
        or payload.e2ee_device_id is not None
    ):
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    channel = await session.get(Channel, (int(source_channel_id), settings.domain))
    if channel is None or channel.unavailable:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    require_channel_federation_access(principal, channel)
    actor = await upsert_remote_user(session, settings, payload.actor)
    if channel.guild_id is not None:
        guild = await home_guild(session, settings, channel.guild_id, for_share=True)
        if (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain):
            raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
        await require_permissions(
            session,
            redis,
            guild,
            actor,
            Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY,
            channel=channel,
        )
    else:
        participant = await session.get(
            DMParticipant,
            (
                channel.id,
                channel.origin_domain,
                actor.id,
                actor.origin_domain,
            ),
        )
        if participant is None:
            raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    source_ref = payload.source_message_ref.resolve(settings.domain)
    source = await session.get(Message, source_ref)
    if (
        source is None
        or source.deleted_at is not None
        or (source.channel_id, source.channel_domain) != (channel.id, channel.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    await require_remote_user_creation_allowed(session, actor)
    poll = await session.get(Poll, (source.id, source.origin_domain))
    if poll is not None or source.message_type not in {0, 19, 20, 23}:
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_NOT_FORWARDABLE"})
    attachments = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.message_id == source.id,
                Attachment.message_domain == source.origin_domain,
                Attachment.deleted_at.is_(None),
            )
            .order_by(Attachment.id, Attachment.origin_domain)
        )
    )
    source_nsfw = await effective_channel_nsfw(session, channel)
    if source_nsfw is None:
        raise HTTPException(status_code=409, detail={"code": "FORWARD_CONTEXT_UNSUPPORTED"})
    try:
        content = build_forward_source_authorization_content(
            source,
            attachments,
            requester_ref=f"{actor.id}@{actor.origin_domain}",
            requester_type="human",
            source_channel_ref=f"{channel.id}@{channel.origin_domain}",
            destination_channel_ref=str(payload.destination_channel_ref),
            destination_encryption_mode=payload.destination_encryption_mode,
            source_nsfw=source_nsfw,
            nonce=payload.nonce,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "FORWARD_SOURCE_PROOF_UNAVAILABLE"},
        ) from exc
    authorization = await build_envelope(
        session,
        settings,
        FORWARD_SOURCE_AUTHORIZATION_EVENT,
        actor,
        content,
        context={"source_channel_ref": f"{channel.id}@{channel.origin_domain}"},
        authority_attested_actor=actor.origin_domain != settings.domain,
    )
    return {"authorization": authorization}


@router.post("/_kaede/v1/dms/{conversation_id}/forward-resolve")
async def federation_dm_forward_resolve(
    conversation_id: Snowflake,
    payload: DMForwardResolveFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Resolve one non-local, live DM source at the conversation authority."""

    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "dm-forward-resolve",
        capacity=1_200,
        refill_per_minute=1_200,
    )
    if payload.requester.domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    conversation = await session.get(DMConversation, (int(conversation_id), settings.domain))
    if conversation is None or conversation.authority_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
    participant = await session.get(
        DMParticipant,
        (
            conversation.id,
            conversation.origin_domain,
            int(payload.requester.id),
            payload.requester.domain,
        ),
    )
    if participant is None:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_DM_HISTORY_FORBIDDEN"})
    source_ref = payload.source_message_ref.resolve(settings.domain)
    source = await session.get(Message, source_ref)
    if (
        source is None
        or source.deleted_at is not None
        or source.e2ee is not None
        or source.origin_domain == principal.origin
        or (source.channel_id, source.channel_domain)
        != (conversation.id, conversation.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
    author = await session.get(User, (source.author_id, source.author_domain))
    if author is None:
        raise RuntimeError("DM forward source author disappeared")
    attachments = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.message_id == source.id,
                Attachment.message_domain == source.origin_domain,
                Attachment.deleted_at.is_(None),
            )
            .order_by(Attachment.id)
        )
    )
    rendered = message_payload(source, author, attachments)
    # This response is fed back through the strict DM-history federation
    # validator.  Replace the client-facing user projection (whose numeric
    # generations are strings) with the canonical federation profile.
    rendered["author"] = profile_from_user(author)
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
    requester = await session.get(
        User,
        (
            int(requester_id if requester_id is not None else participates),
            requester_domain or principal.origin,
        ),
    )
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
    author_refs = {(message.author_id, message.author_domain) for message in selected}
    authors = {
        (author.id, author.origin_domain): author
        for author in await session.scalars(
            select(User).where(tuple_(User.id, User.origin_domain).in_(author_refs))
        )
    }
    reaction_payloads = await reaction_payloads_for_messages(
        session,
        {(message.id, message.origin_domain) for message in selected},
        viewer=requester,
    )
    rendered: list[dict[str, object]] = []
    for message in selected:
        author = authors.get((message.author_id, message.author_domain))
        if author is None:
            raise RuntimeError("DM history message author disappeared")
        item = await render_message_payload(
            session,
            message,
            author,
            viewer=requester,
            include_forward_source=True,
        )
        # DM history is an instance-to-instance protocol response, even though
        # its message body otherwise shares the client renderer.
        item["author"] = profile_from_user(author)
        reaction_counts, reacted_emoji = reaction_payloads.get(
            (message.id, message.origin_domain), ({}, [])
        )
        item["reaction_counts"] = reaction_counts
        item["reacted_emoji"] = reacted_emoji
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
    require_channel_federation_access(principal, channel)
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
    await require_remote_user_creation_allowed(session, inviter)
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
    if payload.action != "leave":
        unlocked_conversation, _ = await load_authoritative_group(
            session,
            settings,
            int(payload.conversation_id),
            payload.conversation_domain,
        )
        await require_group_member(session, unlocked_conversation, actor)
    if payload.action == "add" and target is not None:
        await authorize_group_invitee_at_home(
            session,
            settings,
            unlocked_conversation,
            actor,
            target,
        )
    if payload.action != "leave":
        await require_remote_user_creation_allowed(session, actor)
    if payload.action == "add" and target is not None:
        await require_remote_user_creation_allowed(session, target)
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


async def _authorize_bot_dm_open_capability(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    payload: DMOpenFederationRequest,
    users: list[User],
    *,
    relay_domain: str,
    pair_key: str,
    authority_domain: str,
    runtime_preapplied: bool = False,
    commit_runtime: bool = True,
) -> tuple[EventEnvelope, BotDMCapabilityPayload] | None:
    """Validate a bot install proof, or preserve the ordinary human policy."""

    bots = [user for user in users if user.account_type == "bot"]
    humans = [user for user in users if user.account_type == "human"]
    if payload.bot_capability is None:
        if bots or payload.bot_runtime_proof is not None:
            raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_REQUIRED"})
        return None
    if payload.bot_runtime_proof is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_RUNTIME_REQUIRED"})
    if len(bots) != 1 or len(humans) != 1:
        raise HTTPException(status_code=400, detail={"code": "KAED_DM_INVALID_PARTICIPANTS"})
    bot = bots[0]
    target = humans[0]
    try:
        preliminary_runtime = ApplicationRuntimeSnapshot.model_validate(
            payload.bot_runtime_proof.content
        )
        runtime_application_ref = (
            int(preliminary_runtime.application_id),
            preliminary_runtime.application_domain,
        )
        if settings.domain == authority_domain and (
            relay_domain != bot.origin_domain
            or relay_domain != preliminary_runtime.application_domain
            or preliminary_runtime.bot_user_domain != bot.origin_domain
            or int(preliminary_runtime.bot_user_id) != bot.id
        ):
            raise ValueError("only application home may advance the authority runtime ledger")
        if settings.domain == authority_domain and commit_runtime:
            runtime_envelope, runtime_snapshot = await durably_apply_application_runtime_proof(
                session,
                redis,
                settings,
                expected_origin=bot.origin_domain,
                raw_envelope=payload.bot_runtime_proof,
                application_ref=runtime_application_ref,
                bot=bot,
                target_domain=authority_domain,
            )
        else:
            runtime_envelope, runtime_snapshot = await validate_application_runtime_proof(
                session,
                settings,
                expected_origin=bot.origin_domain,
                raw_envelope=payload.bot_runtime_proof,
                application_ref=runtime_application_ref,
                bot_ref=(bot.id, bot.origin_domain),
                target_domain=authority_domain,
            )
            if settings.domain == authority_domain and not runtime_preapplied:
                await apply_application_runtime_control(
                    session,
                    redis,
                    settings,
                    bot.origin_domain,
                    bot,
                    runtime_snapshot,
                    allow_target_bootstrap=True,
                )
        if (
            settings.domain == authority_domain
            and runtime_snapshot.application_domain != settings.domain
        ):
            await bootstrap_runtime_application_projection(
                session,
                settings,
                snowflake,
                application_id=int(runtime_snapshot.application_id),
                application_domain=runtime_snapshot.application_domain,
                bot_user_id=int(runtime_snapshot.bot_user_id),
                bot_user_domain=runtime_snapshot.bot_user_domain,
                manifest_generation=int(runtime_snapshot.manifest_generation),
                revocation_generation=int(runtime_snapshot.revocation_generation),
                access_revocation_generation=int(runtime_snapshot.access_revocation_generation),
                runtime_snapshot_fingerprint=application_runtime_snapshot_fingerprint(
                    runtime_snapshot
                ),
            )
        proof = await validated_bot_dm_capability_context(
            session,
            settings,
            payload.bot_capability,
            relay_domain=relay_domain,
            bot=bot,
            target=target,
            pair_key=pair_key,
            authority_domain=authority_domain,
        )
        require_capability_runtime_binding(
            proof[1],
            runtime_envelope,
            runtime_snapshot,
        )
        if runtime_snapshot.status != "active" or not runtime_snapshot.target_allowed:
            raise ValueError("bot DM runtime proof does not authorize the conversation")
        if settings.domain in {proof[1].application.domain, authority_domain}:
            await require_stored_capability_runtime(session, settings, proof[1])
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "BOT_DM_AUTHORITY_UNAVAILABLE"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"}) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"}) from exc
    if settings.domain == authority_domain:
        try:
            await validate_bot_dm_capability_at_source(
                session,
                settings,
                proof[0],
                proof[1],
            )
            if proof[1].installation.domain == settings.domain:
                from app.api.bot_dm_federation import require_current_bot_dm_entitlement

                try:
                    await require_current_bot_dm_entitlement(
                        session,
                        settings,
                        proof[1],
                    )
                except HTTPException as exc:
                    raise BotDMCapabilitySourceRejected(
                        "installation authority no longer recognizes the DM grant"
                    ) from exc
        except BotDMCapabilityAuthorityUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "BOT_DM_INSTALLATION_AUTHORITY_UNAVAILABLE"},
            ) from exc
        except BotDMCapabilityProofInvalid as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "BOT_DM_INSTALLATION_PROOF_INVALID"},
            ) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"}) from exc
    # The install authority attests the exact guild/user-install entitlement;
    # the human's home still owns blocks and may atomically reject the bot.
    if target.origin_domain == settings.domain:
        await lock_dm_policy(session, bot, target)
        if await blocked_between(session, bot, target):
            raise HTTPException(status_code=403, detail={"code": "DM_PRIVACY_REJECTED"})
    return proof


@router.post("/_kaede/v1/dm/open")
async def federation_dm_open(
    payload: DMOpenFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "dm-open",
        capacity=120,
        refill_per_minute=120,
    )
    if principal.origin not in {item.origin_domain for item in payload.participants}:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    users = [await upsert_remote_user(session, settings, item) for item in payload.participants]
    local_recipient = next((user for user in users if user.origin_domain == settings.domain), None)
    remote_sender = next((user for user in users if user.origin_domain == principal.origin), None)
    if local_recipient is None or remote_sender is None:
        raise HTTPException(status_code=400, detail={"code": "KAED_DM_INVALID_PARTICIPANTS"})
    await require_remote_user_creation_allowed(session, remote_sender)
    handles = [f"{user.username}@{user.origin_domain}" for user in users]
    pair_key = dm_pair_key(*handles)
    authority = dm_authority_domain(*handles)
    bot_capability = await _authorize_bot_dm_open_capability(
        session,
        redis,
        snowflake,
        settings,
        payload,
        users,
        relay_domain=principal.origin,
        pair_key=pair_key,
        authority_domain=authority,
    )
    if bot_capability is None:
        await require_can_direct_message(session, remote_sender, local_recipient)
    try:
        conversation, channel, users, created = await authoritative_dm_conversation(
            session, settings, snowflake, payload.participants
        )
    except FederatedDMQuotaExceeded as exc:
        raise HTTPException(status_code=507, detail=exc.detail(federation=True)) from exc
    if bot_capability is not None:
        await apply_bot_dm_capability(
            session,
            snowflake,
            bot_capability[0],
            bot_capability[1],
            conversation=conversation,
            runtime_admitted=True,
            admit_fenced_projection=True,
        )
    local_recipient_ref = (local_recipient.id, local_recipient.origin_domain)
    projection = await materialize_dm_open_projection(
        session,
        conversation,
        channel,
        local_recipient_ref=local_recipient_ref,
        created=created,
    )
    await session.commit()
    if created:
        if projection.created_channel is None:
            raise RuntimeError("created DM channel projection is missing")
        await publish_dispatch(
            redis,
            user_topic(settings.domain, local_recipient_ref[0]),
            "CHANNEL_CREATE",
            projection.created_channel,
        )
    return {
        "conversation": projection.conversation,
        "channel": projection.channel,
        "participants": list(projection.participants),
    }


@router.post("/_kaede/v1/dm/authorize")
async def federation_dm_authorize(
    payload: DMOpenFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "dm-authorize",
        capacity=120,
        refill_per_minute=120,
    )
    users = [await upsert_remote_user(session, settings, item) for item in payload.participants]
    local_recipient = next((user for user in users if user.origin_domain == settings.domain), None)
    remote_sender = next((user for user in users if user.origin_domain == principal.origin), None)
    if local_recipient is None or remote_sender is None:
        raise HTTPException(status_code=400, detail={"code": "KAED_DM_INVALID_PARTICIPANTS"})
    await require_remote_user_creation_allowed(session, remote_sender)
    handles = [f"{user.username}@{user.origin_domain}" for user in users]
    bot_capability = await _authorize_bot_dm_open_capability(
        session,
        redis,
        snowflake,
        settings,
        payload,
        users,
        relay_domain=principal.origin,
        pair_key=dm_pair_key(*handles),
        authority_domain=dm_authority_domain(*handles),
    )
    if bot_capability is None:
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
    if invite.target_user_ids and (
        payload.viewer_id is None
        or not invite_allows_user(invite, int(payload.viewer_id), principal.origin)
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    if not active_invite(invite):
        # Permit only the instance that already consumed this invite to recover
        # a lost successful join response. This does not reopen the invite for
        # another user; /join binds replay to the existing composite member.
        prior_member = (
            await session.scalar(
                select(GuildMember.user_id).where(
                    GuildMember.guild_id == invite.guild_id,
                    GuildMember.guild_domain == invite.guild_domain,
                    GuildMember.user_id == int(payload.viewer_id),
                    GuildMember.user_domain == principal.origin,
                )
            )
            if payload.viewer_id is not None
            else None
        )
        if prior_member is None:
            raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    if (
        invite.scheduled_event_id is not None
        and await active_scheduled_event_for_invite(session, invite) is None
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    guild = await home_guild(session, settings, invite.guild_id)
    rendered: dict[str, object] = {
        "code": invite.code,
        "guild": guild_payload(guild),
        "channel_id": str(invite.channel_id) if invite.channel_id is not None else None,
        **invite_target_payload(invite),
    }
    event_payload = await scheduled_event_invite_payload(session, invite)
    if event_payload is not None:
        rendered["guild_scheduled_event"] = event_payload
    return rendered


@router.post("/_kaede/v1/guilds/{guild_id}/join")
async def federation_guild_join(
    guild_id: Snowflake,
    payload: GuildJoinRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "guild-join", capacity=10, refill_per_minute=10
    )
    delivery_destinations: set[str] = set()
    e2ee_policy_channels: list[Channel] = []
    automod_post_commit = AutoModPostCommit()
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
    if not invite_allows_user(invite, user.id, user.origin_domain):
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
    invite_is_active = active_invite(invite)
    scheduled_event_is_active = bool(
        invite.scheduled_event_id is None
        or await active_scheduled_event_for_invite(session, invite) is not None
    )
    if member is None:
        if not invite_is_active:
            raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
        if not scheduled_event_is_active:
            raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
        member = GuildMember(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=user.id,
            user_domain=user.origin_domain,
            joined_at=datetime.now(UTC),
            temporary=invite.temporary,
        )
        session.add(member)
        granted_roles, _newly_granted = await grant_invite_roles(session, guild, member, invite)
        if user.account_type != "bot":
            e2ee_policy_channels.extend(
                await pause_guild_e2ee_for_membership_change(session, guild)
            )
        invite.uses += 1
        guild.snapshot_generation += 1
        seq = await assign_guild_sequence(session, guild)
        owner = await guild_authority_owner(session, settings, guild)
        member_event = await build_guild_authority_envelope(
            session,
            settings,
            guild,
            "guild.member.add",
            owner,
            {
                "user": profile_from_user(user),
                "joined_at": member.joined_at.isoformat(),
                "temporary": member.temporary,
                "role_ids": [
                    {"id": str(role.id), "origin_domain": role.origin_domain}
                    for role in granted_roles
                ],
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
        automod_post_commit = await evaluate_member_profile(
            session,
            settings,
            snowflake,
            guild,
            user,
        )
        await session.commit()
        await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_ADD",
            {
                "guild_id": str(guild.id),
                "user": user_payload(user),
                "role_ids": [str(role.id) for role in granted_roles],
            },
        )
        for destination in delivery_destinations:
            await enqueue_best_effort(federation_deliver, destination)
        await automod_post_commit.publish(redis)
    else:
        newly_granted: list[Role] = []
        if invite_is_active and scheduled_event_is_active:
            _granted_roles, newly_granted = await grant_invite_roles(session, guild, member, invite)
        if newly_granted:
            invite.uses += 1
            owner = await guild_authority_owner(session, settings, guild)
            for role in newly_granted:
                member.member_version += 1
                seq = await queue_guild_mutation(
                    session,
                    settings,
                    guild,
                    owner,
                    "guild.member.role.add",
                    {
                        "user": {
                            "id": str(member.user_id),
                            "origin_domain": member.user_domain,
                        },
                        "role": {
                            "id": str(role.id),
                            "origin_domain": role.origin_domain,
                        },
                        "member_version": str(member.member_version),
                    },
                    snapshot_required=True,
                    e2ee_policy_channels=e2ee_policy_channels,
                )
            role_ids = list(
                await session.scalars(
                    select(MemberRole.role_id).where(
                        MemberRole.guild_id == guild.id,
                        MemberRole.guild_domain == guild.origin_domain,
                        MemberRole.user_id == member.user_id,
                        MemberRole.user_domain == member.user_domain,
                    )
                )
            )
            rendered_member = member_payload(member, user, role_ids)
            await session.commit()
            await wake_queued_guild_federation(guild)
            await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "GUILD_MEMBER_UPDATE",
                rendered_member,
            )
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
    await wake_tracker_membership_cleanup(guild)
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


@router.get("/_kaede/v1/guilds/{guild_id}/trackers/{channel_id}/snapshot")
async def federation_tracker_snapshot(
    guild_id: Snowflake,
    channel_id: Snowflake,
    cursor: str | None = Query(default=None, max_length=1024),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return one bounded, revision-fenced page of a visible tracker board."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-tracker-snapshot",
        capacity=300,
        refill_per_minute=300,
    )
    guild = await home_guild(session, settings, guild_id, for_share=True)
    # An authenticated but unrelated peer must not use channel visibility as
    # an existence oracle. Require a current member hosted by that origin
    # before evaluating the origin-union channel permission graph.
    await require_origin_guild_member(session, guild, principal.origin)
    visible_channels = await cached_visible_guild_channels_for_origin(
        session,
        redis,
        guild,
        principal.origin,
    )
    channel = next(
        (
            item
            for item in visible_channels
            if item.id == int(channel_id)
            and item.origin_domain == guild.origin_domain
            and item.type == 17
        ),
        None,
    )
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_NOT_FOUND"})
    board = await session.scalar(
        select(TrackerBoard)
        .where(
            TrackerBoard.channel_id == channel.id,
            TrackerBoard.channel_domain == channel.origin_domain,
        )
        .with_for_update(read=True)
    )
    if board is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_NOT_FOUND"})
    try:
        after_task_id = (
            tracker_snapshot_cursor_task_id(settings, board, cursor) if cursor is not None else -1
        )
    except TrackerSnapshotChanged:
        raise HTTPException(
            status_code=409,
            detail={"code": "KAED_FED_TRACKER_SNAPSHOT_CHANGED"},
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"code": "KAED_FED_INVALID_TRACKER_CURSOR"},
        ) from None

    lanes = list(
        await session.scalars(
            select(TrackerLane)
            .where(
                TrackerLane.channel_id == board.channel_id,
                TrackerLane.channel_domain == board.channel_domain,
            )
            .order_by(TrackerLane.position, TrackerLane.id)
            .limit(MAX_TRACKER_LANES + 1)
        )
    )
    task_count = int(
        await session.scalar(
            select(func.count())
            .select_from(TrackerTask)
            .where(
                TrackerTask.channel_id == board.channel_id,
                TrackerTask.channel_domain == board.channel_domain,
            )
        )
        or 0
    )
    if not lanes or len(lanes) > MAX_TRACKER_LANES or task_count > MAX_TRACKER_TASKS:
        raise HTTPException(status_code=409, detail={"code": "TRACKER_CAPACITY_INVALID"})
    candidates = list(
        await session.scalars(
            select(TrackerTask)
            .where(
                TrackerTask.channel_id == board.channel_id,
                TrackerTask.channel_domain == board.channel_domain,
                TrackerTask.id > after_task_id,
            )
            .order_by(TrackerTask.id)
            .limit(MAX_TRACKER_PAGE_TASK_CANDIDATES + 1)
        )
    )
    candidate_user_refs = {(task.creator_id, task.creator_domain) for task in candidates} | {
        (task.assignee_id, task.assignee_domain)
        for task in candidates
        if task.assignee_id is not None and task.assignee_domain is not None
    }
    candidate_users = (
        list(
            await session.scalars(
                select(User).where(tuple_(User.id, User.origin_domain).in_(candidate_user_refs))
            )
        )
        if candidate_user_refs
        else []
    )
    users_by_ref = {(user.id, user.origin_domain): user for user in candidate_users}
    if set(users_by_ref) != candidate_user_refs:
        raise HTTPException(status_code=409, detail={"code": "TRACKER_STATE_INVALID"})

    selected: list[TrackerTask] = []
    estimated_bytes = 0
    for task in candidates[:MAX_TRACKER_PAGE_TASK_CANDIDATES]:
        task_size = tracker_snapshot_page_size([task], users_by_ref)
        if selected and estimated_bytes + task_size > TARGET_TRACKER_PAGE_BYTES:
            break
        selected.append(task)
        estimated_bytes += task_size
    has_more = len(selected) < len(candidates)
    selected_user_refs = {(task.creator_id, task.creator_domain) for task in selected} | {
        (task.assignee_id, task.assignee_domain)
        for task in selected
        if task.assignee_id is not None and task.assignee_domain is not None
    }
    selected_assignee_refs = {
        (task.assignee_id, task.assignee_domain)
        for task in selected
        if task.assignee_id is not None and task.assignee_domain is not None
    }
    if selected_assignee_refs:
        authoritative_assignees = set(
            (
                await session.execute(
                    select(GuildMember.user_id, GuildMember.user_domain).where(
                        GuildMember.guild_id == guild.id,
                        GuildMember.guild_domain == guild.origin_domain,
                        tuple_(GuildMember.user_id, GuildMember.user_domain).in_(
                            selected_assignee_refs
                        ),
                    )
                )
            ).tuples()
        )
        if authoritative_assignees != selected_assignee_refs:
            raise HTTPException(status_code=409, detail={"code": "TRACKER_STATE_INVALID"})
    page = tracker_snapshot_page_payload(
        settings,
        board,
        lanes,
        selected,
        [
            users_by_ref[ref]
            for ref in sorted(selected_user_refs, key=lambda item: (item[1], item[0]))
        ],
        task_count=task_count,
        has_more=has_more,
    )
    await record_room_federation_recipient(
        session,
        ("guild", guild.id, guild.origin_domain),
        principal.origin,
    )
    await session.commit()
    return page


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
            owner = await guild_authority_owner(session, settings, guild)
        rendered_events.append(
            await build_guild_authority_envelope(
                session,
                settings,
                guild,
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
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-message-create",
        capacity=3_000,
        refill_per_minute=3_000,
    )
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
    guild = await home_guild(session, settings, guild_id, for_update=True)
    channel = await session.get(Channel, (int(payload.channel_id), guild.origin_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.guild_id != guild.id
        or not is_message_capable_channel_type(channel.type, guild_channel=True)
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    try:
        proxy_nonce_replay = await locked_proxy_nonce_replay(
            session,
            guild,
            channel,
            actor,
            payload,
        )
    except ProxyNonceStateConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "KAED_GUILD_NONCE_STATE_CONFLICT"},
        ) from exc
    if proxy_nonce_replay is not None:
        stored_content = proxy_nonce_replay.event.envelope.get("content")
        stored_message = stored_content.get("message") if isinstance(stored_content, dict) else None
        if not isinstance(stored_message, dict):
            raise HTTPException(
                status_code=409,
                detail={"code": "KAED_GUILD_NONCE_STATE_CONFLICT"},
            )
        return {
            "message": stored_message,
            "seq": str(proxy_nonce_replay.event.seq),
            "event": proxy_nonce_replay.event.envelope,
        }
    await require_remote_user_creation_allowed(session, actor)
    require_message_encryption_policy(
        channel,
        content=payload.content,
        e2ee=payload.e2ee,
        attachment_count=len(payload.attachments),
    )
    encrypted_rich = isinstance(payload.e2ee, dict) and "rich_payload_digest" in payload.e2ee
    encrypted_forward_routing = bool(
        encrypted_rich
        and isinstance(payload.e2ee, dict)
        and payload.e2ee.get("forward_snapshot_digest") is not None
    )
    encrypted_contract, encrypted_controls, encrypted_poll = encrypted_rich_routing(payload.e2ee)
    needed = Permission.VIEW_CHANNEL | (
        Permission.SEND_MESSAGES_IN_THREADS
        if channel.type in {10, 11, 12}
        else Permission.SEND_MESSAGES
    )
    if payload.attachments:
        needed |= Permission.ATTACH_FILES
    if payload.voice_message:
        needed |= Permission.SEND_VOICE_MESSAGES
    if payload.tts:
        needed |= Permission.SEND_TTS_MESSAGES
    if payload.poll is not None or encrypted_poll is not None:
        needed |= Permission.SEND_POLLS
    actor_permissions = await require_permissions(
        session,
        redis,
        guild,
        actor,
        needed,
        channel=channel,
    )
    application_ref = await validated_proxy_application(
        session,
        guild,
        actor,
        payload.application_id,
    )
    if encrypted_forward_routing:
        if payload.expression_authorizations:
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            )
    else:
        try:
            expression_tokens = expression_custom_emoji_tokens(
                content=payload.content,
                components=payload.components,
                poll=payload.poll,
                e2ee=payload.e2ee,
                default_domain=guild.origin_domain,
            )
            attested_tokens, attested_items = await validate_expression_authorization_map(
                session,
                redis,
                settings,
                payload.expression_authorizations,
                requester_ref=f"{actor.id}@{actor.origin_domain}",
                requester_type=cast(Literal["human", "bot"], actor.account_type),
                application_ref=(
                    f"{application_ref[0]}@{application_ref[1]}"
                    if application_ref is not None
                    else None
                ),
                target_guild_ref=f"{guild.id}@{guild.origin_domain}",
                target_channel_ref=f"{channel.id}@{channel.origin_domain}",
                target_message_ref=None,
                operation="message.create",
                operation_id=payload.client_nonce,
                emoji_tokens=expression_tokens,
                sticker_items=payload.sticker_items,
            )
            await validate_attested_expression_target(
                session,
                actor,
                guild,
                actor_permissions,
                attested_tokens,
                attested_items,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            ) from exc
    require_voice_message_attachments(payload.voice_message, payload.attachments)
    await require_voice_message_guild_capacity(
        session,
        guild,
        voice_message=payload.voice_message,
    )
    if (
        channel.type in {10, 11, 12}
        and channel.locked
        and not actor_permissions & Permission.MANAGE_THREADS
    ):
        raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})
    await record_room_federation_recipient(
        session,
        ("guild", guild.id, guild.origin_domain),
        principal.origin,
    )
    await require_proxy_bot_e2ee_participation(
        session,
        guild,
        channel,
        actor,
        application_ref,
        payload.e2ee,
    )
    encrypted_forward = encrypted_forward_routing
    transported_forward = payload.forward_snapshot is not None or encrypted_forward
    forwarded_message = await validated_proxy_forward(
        session,
        redis,
        settings,
        guild,
        channel,
        actor,
        None if transported_forward else payload.forwarded_message_id,
    )
    forward_proof: dict[str, object] | None = None
    if transported_forward:
        if (
            payload.forward_source_nsfw is None
            or payload.forward_source_proof is None
            or payload.forwarded_message_id is None
            or payload.forwarded_channel_id is None
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "FORWARD_SOURCE_PROOF_REQUIRED"},
            )
        forwarded_ref = payload.forwarded_message_id.resolve(settings.domain)
        forwarded_channel_ref = payload.forwarded_channel_id.resolve(settings.domain)
        proof_device_id = (
            cast(str, payload.e2ee.get("sender_device_id"))
            if actor.account_type == "bot"
            and isinstance(payload.e2ee, dict)
            and isinstance(payload.e2ee.get("sender_device_id"), str)
            else None
        )
        forward_proof = await validate_signed_forward_source_proof(
            session,
            settings,
            payload.forward_source_proof,
            requester=actor,
            source_message_ref=forwarded_ref,
            source_channel_ref=forwarded_channel_ref,
            destination_channel=channel,
            nonce=payload.client_nonce,
            application_ref=application_ref,
            e2ee_device_id=proof_device_id,
        )
        if forward_proof["source_nsfw"] is not payload.forward_source_nsfw:
            raise HTTPException(
                status_code=409,
                detail={"code": "FORWARD_SOURCE_PROOF_INVALID"},
            )
        if (
            forward_proof["source_encryption_mode"] == "plaintext"
            and payload.forward_snapshot is not None
            and payload.forward_snapshot != forward_proof["source_snapshot"]
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "FORWARD_SOURCE_PROOF_INVALID"},
            )
        if (
            forward_proof["source_encryption_mode"] == "e2ee"
            and payload.forward_snapshot is not None
        ):
            try:
                require_disclosed_forward_snapshot_proof_binding(
                    payload.forward_snapshot,
                    forward_proof,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "FORWARD_SOURCE_PROOF_INVALID"},
                ) from exc
        try:
            await validate_attested_forward_expressions(
                session,
                actor,
                guild,
                actor_permissions,
                e2ee=payload.e2ee,
                routed_sticker_items=payload.sticker_items,
                forward_snapshot=payload.forward_snapshot,
                forward_proof=forward_proof,
                trusted_external_domain=actor.origin_domain,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "FORWARD_SOURCE_PROOF_INVALID"},
            ) from exc
        await require_attested_forward_age_context(
            session,
            channel,
            payload.forward_source_nsfw,
        )
        source_attachment_count = len(cast(list[str], forward_proof["source_attachment_refs"]))
        if (
            encrypted_forward or forward_proof["source_encryption_mode"] == "e2ee"
        ) and source_attachment_count != len(payload.attachments):
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_FORWARD_ATTACHMENT_MISMATCH"},
            )
    if (
        payload.forward_snapshot is not None
        and payload.attachments
        and not forward_snapshot_matches_attachments(
            payload.forward_snapshot,
            payload.attachments,
        )
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_FORWARD_ATTACHMENT_MISMATCH"},
        )
    referenced_message: Message | None = None
    if payload.referenced_message_id is not None:
        reference = payload.referenced_message_id.resolve(settings.domain)
        referenced_message = await session.get(Message, reference)
        if referenced_message is None or (
            referenced_message.channel_id,
            referenced_message.channel_domain,
        ) != (channel.id, channel.origin_domain):
            raise HTTPException(status_code=400, detail={"code": "INVALID_MESSAGE_REFERENCE"})
    mention_projection = await resolve_proxy_guild_mentions(
        session,
        redis,
        settings,
        guild,
        channel,
        actor,
        actor_permissions,
        payload,
        referenced=referenced_message,
    )
    parsed_mention_refs = list(mention_projection.recipient_refs)
    mention_refs = list(mention_projection.recipient_payload)
    role_mention_recipient_refs = set(mention_projection.role_recipients)
    try:
        interaction_projection = await authoritative_proxy_interaction_projection(
            session,
            settings,
            guild,
            actor,
            application_ref,
            payload,
            referenced_message,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_METADATA_INVALID"},
        ) from exc
    forwarded_created_at = (
        datetime.fromisoformat(cast(str, forward_proof["source_created_at"]))
        if encrypted_forward and forward_proof is not None
        else None
    )
    forwarded_edited_at = (
        datetime.fromisoformat(cast(str, forward_proof["source_edited_at"]))
        if encrypted_forward
        and forward_proof is not None
        and forward_proof.get("source_edited_at") is not None
        else None
    )
    require_encrypted_rich_admission(
        payload.e2ee,
        author=actor,
        attachments=payload.attachments,
        mention_refs=[(int(item["id"]), item["origin_domain"]) for item in mention_refs],
        sticker_items=payload.sticker_items,
        referenced_message_ref=(
            payload.referenced_message_id.resolve(settings.domain)
            if payload.referenced_message_id is not None
            else None
        ),
        application_ref=application_ref,
        installation_lineage=interaction_projection.transport_lineage,
        has_controls=bool(encrypted_controls),
        tts=payload.tts,
        voice_message=payload.voice_message,
        flags=payload.flags,
        view_persistent=payload.view_persistent,
        view_version=1 if encrypted_controls else 0,
        forwarded_message_ref=(
            payload.forwarded_message_id.resolve(settings.domain)
            if payload.forwarded_message_id is not None
            else None
        ),
        forwarded_channel_ref=(
            payload.forwarded_channel_id.resolve(settings.domain)
            if payload.forwarded_channel_id is not None
            else None
        ),
        forward_source_projection_digest=(
            cast(str, forward_proof["source_projection_digest"])
            if encrypted_forward and forward_proof is not None
            else None
        ),
        forwarded_created_at=forwarded_created_at,
        forwarded_edited_at=forwarded_edited_at,
        forwarded_flags=(
            cast(int, forward_proof["source_flags"])
            if encrypted_forward and forward_proof is not None
            else None
        ),
        forwarded_message_type=(
            cast(int, forward_proof["source_message_type"])
            if encrypted_forward and forward_proof is not None
            else None
        ),
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
        if not await proxy_message_matches_request(
            session,
            existing,
            payload,
            application_ref=application_ref,
            installation_lineage=interaction_projection.installation_lineage,
            forwarded_message=forwarded_message,
            mentions=mention_projection,
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "KAED_GUILD_NONCE_STATE_CONFLICT"},
            )
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
    automod_post_commit = await evaluate_automod_message(
        session,
        redis,
        settings,
        snowflake,
        guild,
        channel,
        actor,
        message_automod_text(
            payload.content,
            poll=payload.poll,
            components=payload.components,
        ),
        mention_count=len(mention_refs),
        actor_permissions=actor_permissions,
    )
    message = Message(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=actor.id,
        author_domain=actor.origin_domain,
        content=payload.content,
        e2ee=payload.e2ee,
        tts=payload.tts,
        embeds=[item.model_dump(mode="json", exclude_none=True) for item in payload.embeds],
        components=[item.model_dump(mode="json", exclude_none=True) for item in payload.components],
        sticker_items=payload.sticker_items,
        application_id=application_ref[0] if application_ref is not None else None,
        application_domain=application_ref[1] if application_ref is not None else None,
        view_version=(
            1 if (payload.components or encrypted_controls) and application_ref is not None else 0
        ),
        forwarded_message_id=(
            payload.forwarded_message_id.id if payload.forwarded_message_id is not None else None
        ),
        forwarded_message_domain=(
            payload.forwarded_message_id.resolve(settings.domain)[1]
            if payload.forwarded_message_id is not None
            else None
        ),
        forwarded_channel_id=(
            payload.forwarded_channel_id.id if payload.forwarded_channel_id is not None else None
        ),
        forwarded_channel_domain=(
            payload.forwarded_channel_id.resolve(settings.domain)[1]
            if payload.forwarded_channel_id is not None
            else None
        ),
        forward_snapshot=payload.forward_snapshot,
        encryption_policy_generation=channel.encryption_policy_generation,
        encryption_epoch=channel.encryption_epoch,
        client_nonce=payload.client_nonce,
        referenced_message_id=(referenced_message.id if referenced_message is not None else None),
        referenced_message_domain=(
            referenced_message.origin_domain if referenced_message is not None else None
        ),
        message_type=interaction_projection.message_type,
        interaction_metadata=interaction_projection.metadata,
        mention_user_refs=mention_refs,
        mention_role_refs=[
            {"id": str(role_id), "origin_domain": role_domain}
            for role_id, role_domain in mention_projection.role_refs
        ],
        mention_everyone=mention_projection.everyone,
        flags=(0 if actor_permissions & Permission.EMBED_LINKS else 4)
        | (payload.flags & (MESSAGE_FLAG_SUPPRESS_EMBEDS | MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS))
        | inferred_message_shape_flags(
            voice_message=payload.voice_message,
            components_v2=uses_components_v2(payload.components),
        )
        | (payload.flags & MESSAGE_FLAG_IS_COMPONENTS_V2 if encrypted_rich else 0)
        | (MESSAGE_FLAG_HAS_SNAPSHOT if transported_forward else 0),
    )
    session.add(message)
    await session.flush()
    if payload.poll is not None:
        session.add(
            Poll(
                message_id=message.id,
                message_domain=message.origin_domain,
                question=payload.poll.question.model_dump(mode="json", exclude_none=True),
                allow_multiselect=payload.poll.allow_multiselect,
                layout_type=payload.poll.layout_type,
                expires_at=datetime.now(UTC) + timedelta(hours=payload.poll.duration),
            )
        )
        for answer_id, answer in enumerate(payload.poll.answers, start=1):
            session.add(
                PollAnswer(
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    answer_id=answer_id,
                    text=answer.poll_media.text,
                    emoji=(
                        answer.poll_media.emoji.model_dump(mode="json", exclude_none=True)
                        if answer.poll_media.emoji is not None
                        else None
                    ),
                )
            )
    elif encrypted_poll is not None:
        add_encrypted_poll_rows(session, message, encrypted_poll)
    if (payload.components or encrypted_controls) and application_ref is not None:
        if interaction_projection.installation_lineage is None:
            raise RuntimeError("interactive proxy message lost authority lineage")
        installation_lineage = interaction_projection.installation_lineage
        session.add(
            MessageView(
                message_id=message.id,
                message_domain=message.origin_domain,
                application_id=application_ref[0],
                application_domain=application_ref[1],
                integration_type=installation_lineage[0],
                installation_id=installation_lineage[1],
                installation_domain=installation_lineage[2],
                installation_revision=installation_lineage[3],
                version=1,
                persistent=payload.view_persistent,
                expires_at=(
                    None
                    if payload.view_persistent
                    else datetime.now(UTC)
                    + timedelta(
                        seconds=(
                            cast(int, encrypted_contract["view_timeout_seconds"])
                            if encrypted_contract is not None
                            else payload.view_timeout_seconds or 900
                        )
                    )
                ),
            )
        )
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
            role_mention_recipient_refs,
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
    await replicate_message_attachments(session, settings, message, actor, payload.attachments)
    await advance_channel_cursor(session, channel, message.id, message.origin_domain)
    await session.flush()
    rendered_for_federation = await render_message_payload(
        session,
        message,
        actor,
        viewer=actor,
        include_forward_source=True,
    )
    seq = await assign_guild_sequence(session, guild)
    proxy_receipt = proxy_request_fingerprint_receipt(payload, guild.origin_domain)
    bind_proxy_commit_receipt(message, proxy_receipt, seq)
    owner = await guild_authority_owner(session, settings, guild)
    committed = await build_guild_authority_envelope(
        session,
        settings,
        guild,
        "guild.message.committed",
        owner,
        {
            "message": rendered_for_federation,
            "author": profile_from_user(actor),
            "thread_starter": False,
            "proxy_request_fingerprint": proxy_receipt.wire(),
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
    guild_ref = (guild.id, guild.origin_domain)
    message_ref = (message.id, message.origin_domain)
    thread_projection = (
        await materialize_thread_dispatch(session, channel, added_thread_members)
        if channel.type in {10, 11, 12}
        else None
    )
    await session.flush()
    await session.refresh(message)
    rendered = await render_message_payload(session, message, actor, viewer=actor)
    await session.commit()
    await automod_post_commit.publish(redis)
    if thread_projection is not None:
        thread_topic = guild_topic(thread_projection.guild_ref[1], thread_projection.guild_ref[0])
        await publish_dispatch(
            redis,
            thread_topic,
            "THREAD_UPDATE",
            thread_projection.channel,
        )
        if thread_was_unarchived:
            await publish_current_thread_member_updates(session, redis, guild, channel)
        if thread_projection.added_members:
            for target_ref, rendered_member, _rich_member in thread_projection.added_members:
                await publish_dispatch(
                    redis,
                    thread_topic,
                    "THREAD_CREATE",
                    thread_projection.channel | {"member": rendered_member},
                    audience_user_refs=[target_ref],
                )
                await publish_dispatch(
                    redis,
                    thread_topic,
                    "THREAD_MEMBER_UPDATE",
                    rendered_member,
                    audience_user_refs=[target_ref],
                )
            if thread_projection.members_update is None:
                raise RuntimeError("thread member projection is incomplete")
            await publish_dispatch(
                redis,
                thread_topic,
                "THREAD_MEMBERS_UPDATE",
                thread_projection.members_update,
            )
    await publish_dispatch(
        redis,
        guild_topic(guild_ref[1], guild_ref[0]),
        "MESSAGE_CREATE",
        rendered,
    )
    await enqueue_best_effort(mentions_fanout, *message_ref)
    for destination in remote_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    return {"message": rendered, "seq": str(seq), "event": committed}


async def _announcement_federation_actor(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: FederationPrincipal,
    profile: RemoteUserProfile,
    application_ref: EntityRef | None,
    *,
    actor_intent: object | None = None,
    expected_intent_action: str | None = None,
    expected_intent_resources: dict[str, str] | None = None,
    require_mutation_admission: bool = True,
) -> tuple[User, BotApplication | None]:
    require_guild_federation_access(principal)
    delegated_actor = profile.origin_domain != principal.origin
    if delegated_actor and (
        actor_intent is None or expected_intent_action is None or expected_intent_resources is None
    ):
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    if application_ref is not None:
        resolved_application_ref = application_ref.resolve(settings.domain)
        if not delegated_actor and resolved_application_ref[1] != principal.origin:
            raise HTTPException(
                status_code=403,
                detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"},
            )
        # A signed bot request may identify an already-materialized bot, but it
        # cannot create an ordinary remote user and then relabel it as a bot.
        # Installation federation owns that identity transition and its FKs.
        existing_actor = await session.get(
            User,
            (int(profile.id), profile.origin_domain),
        )
        if existing_actor is None or existing_actor.account_type != "bot":
            raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
        actor = await upsert_remote_user(session, settings, profile)
        application = await session.get(BotApplication, resolved_application_ref)
        if (
            application is None
            or application.status != "active"
            or (application.bot_user_id, application.bot_user_domain)
            != (actor.id, actor.origin_domain)
        ):
            raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
        if delegated_actor:
            try:
                await validate_worker_actor_intent(
                    session,
                    settings.domain,
                    actor_intent,
                    expected_action=cast(str, expected_intent_action),
                    # The nested proof authorizes this receiving resource
                    # authority.  Binding it to the outer relay would let one
                    # intermediary reuse a proof at a different authority.
                    expected_audience=settings.domain,
                    expected_application_ref=resolved_application_ref,
                    expected_actor_ref=(actor.id, actor.origin_domain),
                    expected_resources=cast(dict[str, str], expected_intent_resources),
                    runtime_target_domain=settings.domain,
                    redis=redis,
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "BOT_ACTOR_INTENT_INVALID"},
                ) from exc
        if require_mutation_admission:
            await require_remote_user_creation_allowed(session, actor)
        return actor, application
    if delegated_actor:
        try:
            await validate_human_actor_intent(
                session,
                settings,
                actor_intent,
                expected_action=cast(str, expected_intent_action),
                expected_audience=settings.domain,
                expected_actor_ref=(int(profile.id), profile.origin_domain),
                expected_resources=cast(dict[str, str], expected_intent_resources),
                redis=redis,
            )
        except (FederationNetworkError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "HUMAN_ACTOR_INTENT_INVALID"},
            ) from exc
    actor = await upsert_remote_user(session, settings, profile)
    if require_mutation_admission:
        await require_remote_user_creation_allowed(session, actor)
    if actor.account_type != "human":
        raise HTTPException(status_code=403, detail={"code": "USER_NOT_FOUND"})
    return actor, None


@dataclass(frozen=True, slots=True)
class _FederatedAnnouncementAuth(AuthenticatedUser):
    """Internal actor context; contains public application identity, never a token."""

    application: BotApplication | None = None


def _federated_announcement_auth(
    actor: User,
    application: BotApplication | None,
) -> AuthenticatedUser:
    return _FederatedAnnouncementAuth(
        user=actor,
        grant=cast(AccessGrant, None),
        access_token="",
        cookie_authenticated=False,
        application=application,
    )


@dataclass(frozen=True, slots=True)
class ValidatedAnnouncementSourceProjection:
    message: Message
    attachments: list[dict[str, object]]
    application_ref: tuple[int, str] | None
    view_persistent: bool
    view_expires_at: datetime | None
    interaction_integration_type: str | None
    interaction_installation_ref: tuple[int, str] | None
    interaction_installation_revision: int | None


def validate_announcement_sync_author_profile(
    raw: object,
    *,
    author_ref: tuple[int, str],
    source_deleted: bool,
) -> RemoteUserProfile | None:
    """Bind a crosspost sync's delegated profile to the retained copy author."""

    if raw is None:
        if source_deleted:
            return None
        raise ValueError("announcement crosspost sync is missing its author profile")
    profile = RemoteUserProfile.model_validate(raw)
    if (int(profile.id), profile.origin_domain) != author_ref:
        raise ValueError("announcement crosspost sync substituted its author")
    return profile


def validate_announcement_source_projection(
    raw: dict[str, Any],
    *,
    source_message_ref: tuple[int, str],
    source_channel_ref: tuple[int, str],
    author_ref: tuple[int, str],
) -> ValidatedAnnouncementSourceProjection:
    """Validate the signed body copied into an announcement follower message."""

    source_id = database_snowflake(raw.get("id"), "announcement source message id")
    source_origin = normalize_domain(str(raw.get("origin_domain", "")))
    source_channel_id = database_snowflake(raw.get("channel_id"), "announcement source channel id")
    source_channel_domain = normalize_domain(str(raw.get("channel_domain", "")))
    source_author_id = database_snowflake(raw.get("author_id"), "announcement source author id")
    source_author_domain = normalize_domain(str(raw.get("author_domain", "")))
    message_type = raw.get("message_type", 0)
    flags = raw.get("flags", 0)
    created_at = datetime.fromisoformat(str(raw.get("created_at")))
    content = raw.get("content")
    if created_at.tzinfo is None:
        raise ValueError("announcement source timestamp lacks a timezone")
    if (
        (source_id, source_origin) != source_message_ref
        or (source_channel_id, source_channel_domain) != source_channel_ref
        or (source_author_id, source_author_domain) != author_ref
        or raw.get("deleted_at") is not None
        or raw.get("e2ee") is not None
        or content is not None
        and (not isinstance(content, str) or not 1 <= len(content) <= 4000)
        or isinstance(message_type, bool)
        or not isinstance(message_type, int)
        or message_type not in {0, 19, 20, 23}
        or isinstance(flags, bool)
        or not isinstance(flags, int)
        or flags < 0
        or flags & (MESSAGE_FLAG_IS_CROSSPOST | MESSAGE_FLAG_HAS_SNAPSHOT)
        or raw.get("forwarded_message_id") is not None
        or raw.get("forwarded_message_domain") is not None
        or raw.get("forwarded_channel_id") is not None
        or raw.get("forwarded_channel_domain") is not None
        or raw.get("forward_snapshot") is not None
        or raw.get("poll") is not None
    ):
        raise ValueError("announcement source projection is invalid")
    rich = _validated_message_rich_projection(
        raw,
        message_id=source_id,
        message_origin=source_origin,
        message_created_at=created_at,
        e2ee=None,
        message_type=message_type,
        flags=flags,
    )
    webhook = validate_webhook_attribution(
        raw.get("webhook"),
        message_type=message_type,
        message_origin=source_origin,
        label="announcement source message",
    )
    application_ref = cast(tuple[int, str] | None, rich["application_ref"])
    raw_attachments = raw.get("attachments", [])
    if not isinstance(raw_attachments, list) or any(
        not isinstance(item, dict) for item in raw_attachments
    ):
        raise ValueError("announcement source attachments are invalid")
    view_persistent = raw.get("view_persistent", False)
    raw_view_expiry = raw.get("view_expires_at")
    view_expires_at = (
        datetime.fromisoformat(str(raw_view_expiry)) if raw_view_expiry is not None else None
    )
    if (
        not isinstance(view_persistent, bool)
        or view_expires_at is not None
        and view_expires_at.tzinfo is None
        or view_persistent
        and view_expires_at is not None
        or cast(list[dict[str, Any]], rich["components"])
        and application_ref is not None
        and not view_persistent
        and view_expires_at is None
    ):
        raise ValueError("announcement source view metadata is invalid")
    source = Message(
        id=source_id,
        origin_domain=source_origin,
        channel_id=source_channel_id,
        channel_domain=source_channel_domain,
        author_id=author_ref[0],
        author_domain=author_ref[1],
        content=content,
        e2ee=None,
        embeds=cast(list[dict[str, Any]], rich["embeds"]),
        components=cast(list[dict[str, Any]], rich["components"]),
        sticker_items=cast(list[dict[str, Any]], rich["sticker_items"]),
        application_id=application_ref[0] if application_ref is not None else None,
        application_domain=application_ref[1] if application_ref is not None else None,
        view_version=int(raw.get("view_version", 0)),
        message_type=message_type,
        flags=flags,
        webhook_id=webhook.webhook_ref[0] if webhook is not None else None,
        webhook_domain=webhook.webhook_ref[1] if webhook is not None else None,
        webhook_name=webhook.name if webhook is not None else None,
        webhook_avatar_hash=webhook.avatar_hash if webhook is not None else None,
        webhook_avatar_url=webhook.avatar_url if webhook is not None else None,
        created_at=created_at,
    )
    projection = ValidatedAnnouncementSourceProjection(
        message=source,
        attachments=[cast(dict[str, object], item) for item in raw_attachments],
        application_ref=application_ref,
        view_persistent=view_persistent,
        view_expires_at=view_expires_at,
        interaction_integration_type=cast(
            str | None,
            rich["interaction_integration_type"],
        ),
        interaction_installation_ref=cast(
            tuple[int, str] | None,
            rich["interaction_installation_ref"],
        ),
        interaction_installation_revision=cast(
            int | None,
            rich["interaction_installation_revision"],
        ),
    )
    validated_announcement_copy_view_projection(projection)
    return projection


def validated_announcement_copy_view_projection(
    source: ValidatedAnnouncementSourceProjection,
) -> AnnouncementCopyViewProjection | None:
    """Convert a validated wire view into the shared copy lifecycle shape."""

    identity = (
        source.interaction_integration_type,
        source.interaction_installation_ref,
        source.interaction_installation_revision,
    )
    if identity == (None, None, None):
        if source.message.components and source.application_ref is not None:
            raise ValueError("announcement component view identity is missing")
        return None
    if any(value is None for value in identity) or source.application_ref is None:
        raise ValueError("announcement component view identity is incomplete")
    return AnnouncementCopyViewProjection(
        application_ref=source.application_ref,
        integration_type=cast(str, source.interaction_integration_type),
        installation_ref=cast(tuple[int, str], source.interaction_installation_ref),
        installation_revision=cast(int, source.interaction_installation_revision),
        version=int(source.message.view_version or 1),
        persistent=source.view_persistent,
        expires_at=source.view_expires_at,
    )


@router.post("/_kaede/v1/channels/{source_channel_id}/announcement-follow-source-authorize")
async def federation_authorize_announcement_follow_source(
    source_channel_id: Snowflake,
    payload: AnnouncementFollowSourceAuthorizeRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "announcement-follow-source-authorize",
        capacity=120,
        refill_per_minute=120,
    )
    actor, application = await _announcement_federation_actor(
        session,
        redis,
        settings,
        principal,
        payload.actor,
        payload.actor_application_ref,
        actor_intent=actor_intent_for_authority(
            payload.actor_intent,
            payload.actor_intents,
            settings.domain,
        ),
        expected_intent_action="announcement.follow.create",
        expected_intent_resources={
            "source_channel": f"{source_channel_id}@{settings.domain}",
            "target_channel": str(payload.target_channel_ref),
        },
    )
    return await authorize_federated_announcement_follow_source(
        session,
        redis,
        settings,
        actor,
        application,
        EntityRef(f"{source_channel_id}@{settings.domain}"),
        payload.target_channel_ref,
    )


@router.post(
    "/_kaede/v1/channels/{target_channel_id}/announcement-follow-authorize",
    status_code=201,
)
async def federation_authorize_announcement_follow(
    target_channel_id: Snowflake,
    payload: AnnouncementFollowAuthorizeRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "announcement-follow-authorize",
        capacity=120,
        refill_per_minute=120,
    )
    if int(payload.target_channel_id) != int(target_channel_id):
        raise HTTPException(status_code=422, detail={"code": "CHANNEL_REF_MISMATCH"})
    if payload.source_authorization is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "ANNOUNCEMENT_SOURCE_AUTHORIZATION_REQUIRED"},
        )
    source_ref = payload.source_channel_ref.resolve(settings.domain)
    await validated_federated_announcement_follow_source_authorization(
        session,
        settings,
        (int(payload.actor.id), payload.actor.origin_domain),
        source_ref,
        (int(target_channel_id), settings.domain),
        payload.source_authorization,
    )
    actor, application = await _announcement_federation_actor(
        session,
        redis,
        settings,
        principal,
        payload.actor,
        payload.actor_application_ref,
        actor_intent=actor_intent_for_authority(
            payload.actor_intent,
            payload.actor_intents,
            settings.domain,
        ),
        expected_intent_action="announcement.follow.create",
        expected_intent_resources={
            "source_channel": str(payload.source_channel_ref),
            "target_channel": f"{target_channel_id}@{settings.domain}",
        },
    )
    return await authorize_federated_announcement_follow_target(
        session,
        redis,
        snowflake,
        settings,
        actor,
        application,
        payload.source_channel_ref,
        EntityRef(f"{target_channel_id}@{settings.domain}"),
        payload.source_authorization,
    )


@router.post(
    "/_kaede/v1/channels/{source_channel_id}/announcement-follow-accept",
    status_code=201,
)
async def federation_accept_announcement_follow(
    source_channel_id: Snowflake,
    payload: AnnouncementFollowAcceptRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "announcement-follow-accept",
        capacity=120,
        refill_per_minute=120,
    )
    require_guild_federation_access(principal)
    raw_content = payload.receipt.get("content")
    try:
        receipt_origin = normalize_domain(str(payload.receipt.get("origin", "")))
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "ANNOUNCEMENT_FOLLOW_RECEIPT_INVALID"},
        ) from exc
    if not isinstance(raw_content, dict) or raw_content.get("source_channel_ref") != (
        f"{source_channel_id}@{settings.domain}"
    ):
        raise HTTPException(status_code=422, detail={"code": "CHANNEL_REF_MISMATCH"})
    if receipt_origin != principal.origin or receipt_origin == settings.domain:
        raise HTTPException(
            status_code=403,
            detail={"code": "ANNOUNCEMENT_FOLLOW_RECEIPT_MISMATCH"},
        )
    result = await accept_federated_announcement_follow_source(
        session,
        redis,
        settings,
        payload.receipt,
    )
    if result.get("source_channel_id") != str(source_channel_id):
        raise HTTPException(status_code=422, detail={"code": "CHANNEL_REF_MISMATCH"})
    return result


@router.post(
    "/_kaede/v1/channels/{source_channel_id}/announcement-follow-create",
    status_code=201,
)
async def federation_create_local_announcement_follow(
    source_channel_id: Snowflake,
    payload: AnnouncementFollowAuthorizeRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Create a normal follow when both guilds share this remote authority."""

    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "announcement-follow-create",
        capacity=120,
        refill_per_minute=120,
    )
    source_ref = payload.source_channel_ref.resolve(settings.domain)
    if source_ref != (int(source_channel_id), settings.domain):
        raise HTTPException(status_code=422, detail={"code": "CHANNEL_REF_MISMATCH"})
    actor, application = await _announcement_federation_actor(
        session,
        redis,
        settings,
        principal,
        payload.actor,
        payload.actor_application_ref,
        actor_intent=actor_intent_for_authority(
            payload.actor_intent,
            payload.actor_intents,
            settings.domain,
        ),
        expected_intent_action="announcement.follow.create",
        expected_intent_resources={
            "source_channel": f"{source_channel_id}@{settings.domain}",
            "target_channel": f"{payload.target_channel_id}@{settings.domain}",
        },
    )
    auth = _federated_announcement_auth(actor, application)
    return await follow_announcement_channel(
        EntityRef(f"{source_channel_id}@{settings.domain}"),
        ChannelFollowCreate(
            target_channel_id=EntityRef(f"{payload.target_channel_id}@{settings.domain}")
        ),
        auth,
        session,
        redis,
        snowflake,
        settings,
    )


@router.post("/_kaede/v1/channels/{source_channel_id}/announcement-follow-list")
async def federation_list_announcement_follows(
    source_channel_id: Snowflake,
    payload: AnnouncementFollowActorRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "announcement-follow-list",
        capacity=300,
        refill_per_minute=300,
    )
    actor, application = await _announcement_federation_actor(
        session,
        redis,
        settings,
        principal,
        payload.actor,
        payload.actor_application_ref,
        actor_intent=actor_intent_for_authority(
            payload.actor_intent,
            payload.actor_intents,
            settings.domain,
        ),
        expected_intent_action="announcement.follow.list",
        expected_intent_resources={
            "source_channel": f"{source_channel_id}@{settings.domain}",
        },
        require_mutation_admission=False,
    )
    auth = _federated_announcement_auth(actor, application)
    return await list_announcement_follows(
        EntityRef(f"{source_channel_id}@{settings.domain}"),
        auth,
        session,
        redis,
        settings,
    )


@router.post(
    "/_kaede/v1/channels/{target_channel_id}/announcement-follow-revoke",
    status_code=200,
)
async def federation_revoke_announcement_follow(
    target_channel_id: Snowflake,
    payload: AnnouncementFollowRevokeRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "announcement-follow-revoke",
        capacity=120,
        refill_per_minute=120,
    )
    follow = await session.get(
        FederatedChannelFollow,
        federated_follow_key(int(payload.follow_id), settings.domain, "target"),
    )
    if follow is None or follow.target_channel_id != int(target_channel_id):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
    actor, application = await _announcement_federation_actor(
        session,
        redis,
        settings,
        principal,
        payload.actor,
        payload.actor_application_ref,
        actor_intent=actor_intent_for_authority(
            payload.actor_intent,
            payload.actor_intents,
            settings.domain,
        ),
        expected_intent_action="announcement.follow.delete",
        expected_intent_resources={
            "source_channel": f"{follow.source_channel_id}@{follow.source_channel_domain}",
            "follow_id": qualified_follow_ref(follow.id, follow.target_authority_domain),
        },
        require_mutation_admission=False,
    )
    return await revoke_federated_announcement_follow_target(
        session,
        redis,
        settings,
        actor,
        application,
        int(payload.follow_id),
        int(payload.generation),
    )


@router.post(
    "/_kaede/v1/channels/{source_channel_id}/announcement-follow-deactivate",
    status_code=204,
)
async def federation_deactivate_announcement_follow(
    source_channel_id: Snowflake,
    payload: AnnouncementFollowDeactivateRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "announcement-follow-deactivate",
        capacity=120,
        refill_per_minute=120,
    )
    require_guild_federation_access(principal)
    raw_content = payload.receipt.get("content")
    try:
        receipt_origin = normalize_domain(str(payload.receipt.get("origin", "")))
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "ANNOUNCEMENT_FOLLOW_RECEIPT_INVALID"},
        ) from exc
    if not isinstance(raw_content, dict) or raw_content.get("source_channel_ref") != (
        f"{source_channel_id}@{settings.domain}"
    ):
        raise HTTPException(status_code=422, detail={"code": "CHANNEL_REF_MISMATCH"})
    if receipt_origin != principal.origin or receipt_origin == settings.domain:
        raise HTTPException(
            status_code=403,
            detail={"code": "ANNOUNCEMENT_FOLLOW_RECEIPT_MISMATCH"},
        )
    await deactivate_federated_announcement_follow_source(
        session,
        settings,
        payload.receipt,
    )
    return Response(status_code=204)


@router.post(
    "/_kaede/v1/channels/{source_channel_id}/announcement-follow-delete/{follow_ref}",
    status_code=204,
)
async def federation_delete_local_announcement_follow(
    source_channel_id: Snowflake,
    follow_ref: EntityRef,
    payload: AnnouncementFollowActorRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "announcement-follow-delete",
        capacity=120,
        refill_per_minute=120,
    )
    actor, application = await _announcement_federation_actor(
        session,
        redis,
        settings,
        principal,
        payload.actor,
        payload.actor_application_ref,
        actor_intent=actor_intent_for_authority(
            payload.actor_intent,
            payload.actor_intents,
            settings.domain,
        ),
        expected_intent_action="announcement.follow.delete",
        expected_intent_resources={
            "source_channel": f"{source_channel_id}@{settings.domain}",
            "follow_id": str(follow_ref),
        },
        require_mutation_admission=False,
    )
    auth = _federated_announcement_auth(actor, application)
    return await delete_announcement_follow(
        EntityRef(f"{source_channel_id}@{settings.domain}"),
        follow_ref,
        auth,
        session,
        redis,
        settings,
        actor_intent=payload.actor_intent,
        actor_intents=payload.actor_intents,
    )


@router.post(
    "/_kaede/v1/channels/{target_channel_id}/announcement-crossposts",
    status_code=201,
)
async def federation_deliver_announcement_crosspost(
    target_channel_id: Snowflake,
    payload: AnnouncementCrosspostDeliverRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Commit one idempotent live reference under the target's signed grant."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "announcement-crosspost-deliver",
        capacity=600,
        refill_per_minute=600,
    )
    follow_id = int(payload.follow_id)
    generation = int(payload.generation)
    source_ref = payload.source_channel_ref.resolve(settings.domain)
    source_message_ref = payload.source_message_ref.resolve(settings.domain)
    if source_ref[1] != principal.origin or source_message_ref[1] != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    unlocked_follow = await session.get(
        FederatedChannelFollow,
        federated_follow_key(follow_id, settings.domain, "target"),
    )
    if (
        unlocked_follow is None
        or (unlocked_follow.source_channel_id, unlocked_follow.source_channel_domain) != source_ref
        or unlocked_follow.source_authority_domain != principal.origin
    ):
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"})
    target = await session.get(Channel, (int(target_channel_id), settings.domain))
    if target is None or target.unavailable or target.type != 0 or target.guild_id is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    await lock_announcement_mutation(session)
    lock_key = (
        f"announcement-crosspost:{qualified_follow_ref(follow_id, settings.domain)}:"
        f"{source_message_ref[0]}@{principal.origin}"
    )
    await session.scalar(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))
    guild = await session.get(
        Guild,
        (target.guild_id, target.guild_domain),
        with_for_update=True,
    )
    await session.refresh(target)
    follow = await session.get(
        FederatedChannelFollow,
        federated_follow_key(follow_id, settings.domain, "target"),
        with_for_update=True,
        populate_existing=True,
    )
    published_at = payload.published_at.astimezone(UTC)
    current = datetime.now(UTC)
    existing = await session.get(
        FederatedMessageCrosspost,
        federated_crosspost_key(
            source_message_ref[0],
            source_message_ref[1],
            follow_id,
            settings.domain,
            "target",
        ),
        with_for_update=True,
    )
    if existing is not None:
        if (
            follow is None
            or existing.generation != generation
            or existing.delivery_status != "delivered"
            or existing.destination_message_id is None
            or existing.destination_message_domain is None
            or (follow.source_channel_id, follow.source_channel_domain) != source_ref
            or follow.source_authority_domain != principal.origin
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"},
            )
        destination = await session.get(
            Message,
            (existing.destination_message_id, existing.destination_message_domain),
        )
        if destination is None or destination.deleted_at is not None:
            raise HTTPException(
                status_code=410,
                detail={"code": "CROSSPOST_DESTINATION_DELETED"},
            )
        if (
            (destination.channel_id, destination.channel_domain)
            != (int(target_channel_id), settings.domain)
            or (destination.forwarded_message_id, destination.forwarded_message_domain)
            != source_message_ref
            or (destination.forwarded_channel_id, destination.forwarded_channel_domain)
            != source_ref
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"},
            )
        return {
            "destination_message_ref": f"{destination.id}@{destination.origin_domain}",
            "message": await render_message_payload(session, destination),
        }
    active_grant = bool(
        follow is not None
        and follow.active
        and follow.lifecycle_state == "active"
        and follow.generation == generation
    )
    revoked_grant = bool(
        follow is not None
        and follow.lifecycle_state == "revoked"
        and follow.revoked_at is not None
        and generation == follow.generation - 1
        and published_at <= follow.revoked_at
    )
    if (
        follow is None
        or not (active_grant or revoked_grant)
        or (follow.source_channel_id, follow.source_channel_domain) != source_ref
        or follow.source_authority_domain != principal.origin
        or follow.target_channel_id != int(target_channel_id)
        or follow.target_channel_domain != settings.domain
        or published_at > current + timedelta(minutes=1)
        or follow.activated_at is not None
        and published_at < follow.activated_at - timedelta(minutes=1)
    ):
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"})
    if (
        guild is None
        or guild.origin_domain != settings.domain
        or guild.unavailable
        or target.unavailable
        or target.type != 0
        or target.origin_domain != settings.domain
        or (target.guild_id, target.guild_domain) != (guild.id, guild.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    if target.encryption_mode == "e2ee" or target.e2ee_required:
        raise HTTPException(status_code=409, detail={"code": "E2EE_CROSSPOST_UNSUPPORTED"})
    author = await resolve_delegated_profile(
        session,
        settings,
        payload.source_author,
        authority_origin=principal.origin,
    )
    application_ref = (
        payload.application_ref.resolve(settings.domain)
        if payload.application_ref is not None
        else None
    )
    raw_source = payload.source_message
    try:
        validated_source = validate_announcement_source_projection(
            raw_source,
            source_message_ref=source_message_ref,
            source_channel_ref=source_ref,
            author_ref=(author.id, author.origin_domain),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "ANNOUNCEMENT_SOURCE_PROJECTION_INVALID"},
        ) from exc
    if application_ref != validated_source.application_ref:
        raise HTTPException(
            status_code=422,
            detail={"code": "ANNOUNCEMENT_SOURCE_PROJECTION_INVALID"},
        )
    source_shadow = validated_source.message
    delivered_at = datetime.now(UTC)
    destination = Message(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        channel_id=target.id,
        channel_domain=target.origin_domain,
        author_id=author.id,
        author_domain=author.origin_domain,
        content=source_shadow.content,
        e2ee=None,
        embeds=list(source_shadow.embeds or []),
        components=list(source_shadow.components or []),
        sticker_items=list(source_shadow.sticker_items or []),
        encryption_policy_generation=target.encryption_policy_generation,
        encryption_epoch=target.encryption_epoch,
        message_type=0,
        flags=MESSAGE_FLAG_IS_CROSSPOST,
        forwarded_message_id=source_message_ref[0],
        forwarded_message_domain=source_message_ref[1],
        forwarded_channel_id=source_ref[0],
        forwarded_channel_domain=source_ref[1],
        application_id=application_ref[0] if application_ref is not None else None,
        application_domain=application_ref[1] if application_ref is not None else None,
        view_version=int(source_shadow.view_version or 0),
    )
    apply_announcement_copy_projection(
        destination,
        source_shadow,
        changed_at=delivered_at,
        source_deleted=False,
        initial=True,
    )
    known_source_channel = await session.get(Channel, source_ref)
    known_source_guild = (
        await session.get(
            Guild,
            (known_source_channel.guild_id, known_source_channel.guild_domain),
        )
        if known_source_channel is not None and known_source_channel.guild_id is not None
        else None
    )
    apply_announcement_follower_attribution(
        destination,
        follow,
        default_name=(
            known_source_guild.name if known_source_guild is not None else "Channel Follower"
        ),
    )
    session.add(destination)
    await session.flush()
    destination_attachments = await replicate_message_attachments(
        session,
        settings,
        destination,
        author,
        validated_source.attachments,
        allowed_attachment_origins={author.origin_domain, principal.origin},
    )
    destination_view = await sync_target_announcement_copy_view(
        session,
        guild,
        destination,
        None,
        validated_announcement_copy_view_projection(validated_source),
    )
    session.add(
        FederatedMessageCrosspost(
            source_message_id=source_message_ref[0],
            source_message_domain=source_message_ref[1],
            follow_id=follow.id,
            follow_authority_domain=follow.target_authority_domain,
            local_role="target",
            generation=generation,
            destination_message_id=destination.id,
            destination_message_domain=destination.origin_domain,
            delivery_status="delivered",
            attempts=1,
            next_retry_at=delivered_at,
            published_at=published_at,
        )
    )
    session.add(
        MessageProjection(
            message_id=destination.id,
            message_domain=destination.origin_domain,
            channel_id=target.id,
            channel_domain=target.origin_domain,
            mention_user_refs=[],
        )
    )
    rendered = message_payload(
        destination,
        author,
        destination_attachments,
        view=destination_view,
    )
    crosspost_signer = await guild_authority_owner(session, settings, guild)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        crosspost_signer,
        "guild.message.create",
        {
            "message": rendered,
            "author": profile_from_user(author),
            "thread_starter": False,
        },
        channel=target,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "MESSAGE_CREATE",
        rendered,
    )
    return {
        "destination_message_ref": f"{destination.id}@{destination.origin_domain}",
        "message": rendered,
    }


@router.post("/_kaede/v1/channels/{source_channel_id}/announcement-crossposts/resolve")
async def federation_resolve_announcement_crosspost_source(
    source_channel_id: Snowflake,
    payload: AnnouncementCrosspostResolveRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "announcement-crosspost-resolve",
        capacity=1_200,
        refill_per_minute=1_200,
    )
    source_ref = payload.source_message_ref.resolve(settings.domain)
    follow = await session.get(
        FederatedChannelFollow,
        federated_follow_key(int(payload.follow_id), principal.origin, "source"),
    )
    receipt = await session.get(
        FederatedMessageCrosspost,
        federated_crosspost_key(
            source_ref[0],
            source_ref[1],
            int(payload.follow_id),
            principal.origin,
            "source",
        ),
    )
    if (
        follow is None
        or follow.target_authority_domain != principal.origin
        or follow.source_channel_id != int(source_channel_id)
        or follow.source_channel_domain != settings.domain
        or source_ref[1] != settings.domain
        or receipt is None
        or receipt.generation != int(payload.generation)
        or receipt.delivery_status != "delivered"
    ):
        raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
    source = await session.get(Message, source_ref)
    if (
        source is None
        or source.deleted_at is not None
        or source.e2ee is not None
        or (source.channel_id, source.channel_domain)
        != (follow.source_channel_id, follow.source_channel_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
    rendered = await render_message_payload(session, source)
    rendered["source_channel_ref"] = f"{source.channel_id}@{source.channel_domain}"
    return rendered


@router.post("/_kaede/v1/guilds/{guild_id}/proxy-forward-resolve")
async def federation_guild_forward_resolve(
    guild_id: Snowflake,
    payload: GuildForwardResolveRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Resolve a live forward at the destination guild's authority.

    Announcement grants are intentionally kept at the authority rather than
    copied to every member home. This endpoint rechecks target visibility and
    either the durable follow receipt or ordinary source-channel visibility on
    every resolution, so deletes and permission changes take effect instantly.
    """

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-forward-resolve",
        capacity=600,
        refill_per_minute=600,
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    guild = await home_guild(session, settings, guild_id, for_share=True)
    channel = await session.get(Channel, (int(payload.channel_id), guild.origin_domain))
    if (
        channel is None
        or channel.unavailable
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        or not is_message_capable_channel_type(channel.type, guild_channel=True)
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    await require_permissions(
        session,
        redis,
        guild,
        actor,
        Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY,
        channel=channel,
    )
    destination_id, destination_domain = payload.message_id.resolve(settings.domain)
    destination = await session.get(Message, (destination_id, destination_domain))
    if (
        destination is None
        or destination.deleted_at is not None
        or (destination.channel_id, destination.channel_domain)
        != (channel.id, channel.origin_domain)
        or destination.forwarded_message_id is None
        or destination.forwarded_message_domain is None
    ):
        raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
    federated_crosspost = await session.scalar(
        select(FederatedMessageCrosspost).where(
            FederatedMessageCrosspost.destination_message_id == destination.id,
            FederatedMessageCrosspost.destination_message_domain == destination.origin_domain,
            FederatedMessageCrosspost.local_role == "target",
        )
    )
    if federated_crosspost is not None:
        follow = await session.get(
            FederatedChannelFollow,
            federated_follow_key(
                federated_crosspost.follow_id,
                federated_crosspost.follow_authority_domain,
                "target",
            ),
        )
        if follow is None or (
            federated_crosspost.source_message_id,
            federated_crosspost.source_message_domain,
        ) != (destination.forwarded_message_id, destination.forwarded_message_domain):
            raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
        try:
            upstream = await signed_request(
                session,
                settings,
                "POST",
                follow.source_authority_domain,
                (f"/_kaede/v1/channels/{follow.source_channel_id}/announcement-crossposts/resolve"),
                payload={
                    "follow_id": str(follow.id),
                    "generation": str(federated_crosspost.generation),
                    "source_message_ref": (
                        f"{federated_crosspost.source_message_id}@"
                        f"{federated_crosspost.source_message_domain}"
                    ),
                },
                request_timeout=10,
                max_response_bytes=512 * 1024,
            )
        except FederationNetworkError:
            raise HTTPException(
                status_code=503,
                detail={"code": "FEDERATED_FORWARD_UNAVAILABLE"},
            ) from None
        if upstream.status_code == 404:
            raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
        if upstream.status_code != 200:
            raise HTTPException(
                status_code=503,
                detail={"code": "FEDERATED_FORWARD_UNAVAILABLE"},
            )
        remote = decode_federation_response_json(upstream)
        if (
            not isinstance(remote, dict)
            or remote.get("id") != str(federated_crosspost.source_message_id)
            or remote.get("origin_domain") != federated_crosspost.source_message_domain
            or remote.get("source_channel_ref")
            != f"{follow.source_channel_id}@{follow.source_channel_domain}"
        ):
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_FORWARD_RESPONSE_INVALID"},
            )
        return {str(key): value for key, value in remote.items()}
    source = await session.get(
        Message,
        (destination.forwarded_message_id, destination.forwarded_message_domain),
    )
    if source is None or source.deleted_at is not None or source.e2ee is not None:
        raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
    announcement_grant = await session.scalar(
        select(MessageCrosspost.follow_id)
        .join(ChannelFollow, ChannelFollow.id == MessageCrosspost.follow_id)
        .where(
            MessageCrosspost.destination_message_id == destination.id,
            MessageCrosspost.destination_message_domain == destination.origin_domain,
            MessageCrosspost.source_message_id == source.id,
            MessageCrosspost.source_message_domain == source.origin_domain,
            ChannelFollow.source_channel_id == source.channel_id,
            ChannelFollow.source_channel_domain == source.channel_domain,
            ChannelFollow.target_channel_id == destination.channel_id,
            ChannelFollow.target_channel_domain == destination.channel_domain,
        )
    )
    if announcement_grant is None:
        source_channel = await session.get(Channel, (source.channel_id, source.channel_domain))
        if (
            source_channel is None
            or source_channel.unavailable
            or (source_channel.guild_id, source_channel.guild_domain)
            != (guild.id, guild.origin_domain)
        ):
            raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
        await require_permissions(
            session,
            redis,
            guild,
            actor,
            Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY,
            channel=source_channel,
        )
    rendered = await render_message_payload(session, source, viewer=actor)
    rendered["source_channel_ref"] = f"{source.channel_id}@{source.channel_domain}"
    return rendered


async def _federation_poll_context(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild_id: int,
    actor: User,
    channel_id: int,
    message_ref: EntityRef,
    *,
    for_update: bool,
) -> tuple[Guild, Channel, Message, Poll]:
    guild = await home_guild(session, settings, guild_id, for_update=for_update)
    channel = await session.get(Channel, (channel_id, guild.origin_domain))
    if (
        channel is None
        or channel.unavailable
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        or not is_message_capable_channel_type(channel.type, guild_channel=True)
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    await require_permissions(
        session,
        redis,
        guild,
        actor,
        Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY,
        channel=channel,
    )
    message_id, message_domain = message_ref.resolve(settings.domain)
    message = await session.get(Message, (message_id, message_domain))
    if (
        message is None
        or message.deleted_at is not None
        or (message.channel_id, message.channel_domain) != (channel.id, channel.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    poll_statement = select(Poll).where(
        Poll.message_id == message.id,
        Poll.message_domain == message.origin_domain,
    )
    if for_update:
        poll_statement = poll_statement.with_for_update()
    poll = await session.scalar(poll_statement)
    if poll is None:
        raise HTTPException(status_code=404, detail={"code": "POLL_NOT_FOUND"})
    return guild, channel, message, poll


@router.post("/_kaede/v1/guilds/{guild_id}/proxy-poll-vote")
async def federation_guild_poll_vote_proxy(
    guild_id: Snowflake,
    payload: GuildPollVoteProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-poll-vote",
        capacity=300,
        refill_per_minute=300,
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    guild, channel, message, poll = await _federation_poll_context(
        session,
        redis,
        settings,
        int(guild_id),
        actor,
        int(payload.channel_id),
        payload.message_id,
        for_update=True,
    )
    if not payload.remove:
        await require_remote_user_creation_allowed(session, actor)
        await require_member_interactions_allowed(
            session,
            guild,
            actor,
            Permission.SEND_POLLS,
        )
    if poll.finalized_at is not None or poll.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=409, detail={"code": "POLL_FINALIZED"})
    answer = await session.get(
        PollAnswer,
        (message.id, message.origin_domain, payload.answer_id),
    )
    if answer is None:
        raise HTTPException(status_code=404, detail={"code": "POLL_ANSWER_NOT_FOUND"})
    removed_answers: list[int] = []
    changed = False
    if payload.remove:
        removed = await session.scalar(
            delete(PollVote)
            .where(
                PollVote.message_id == message.id,
                PollVote.message_domain == message.origin_domain,
                PollVote.answer_id == payload.answer_id,
                PollVote.user_id == actor.id,
                PollVote.user_domain == actor.origin_domain,
            )
            .returning(PollVote.answer_id)
        )
        changed = removed is not None
        if removed is not None:
            removed_answers.append(int(removed))
    else:
        if not poll.allow_multiselect:
            removed_answers = list(
                await session.scalars(
                    delete(PollVote)
                    .where(
                        PollVote.message_id == message.id,
                        PollVote.message_domain == message.origin_domain,
                        PollVote.user_id == actor.id,
                        PollVote.user_domain == actor.origin_domain,
                        PollVote.answer_id != payload.answer_id,
                    )
                    .returning(PollVote.answer_id)
                )
            )
        inserted = await session.scalar(
            pg_insert(PollVote)
            .values(
                message_id=message.id,
                message_domain=message.origin_domain,
                answer_id=payload.answer_id,
                user_id=actor.id,
                user_domain=actor.origin_domain,
            )
            .on_conflict_do_nothing()
            .returning(PollVote.answer_id)
        )
        changed = inserted is not None or bool(removed_answers)
    if changed:
        await mark_guild_activity(
            session,
            settings,
            ChannelAccess(channel=channel, guild=guild, participants=[]),
            actor,
        )
        for removed_answer in removed_answers:
            await queue_guild_mutation(
                session,
                settings,
                guild,
                actor,
                "guild.poll.vote.remove",
                {
                    "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                    "user": {"id": str(actor.id), "origin_domain": actor.origin_domain},
                    "answer_id": removed_answer,
                },
                channel=channel,
            )
        if not payload.remove and inserted is not None:
            await queue_guild_mutation(
                session,
                settings,
                guild,
                actor,
                "guild.poll.vote.add",
                {
                    "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                    "user": {"id": str(actor.id), "origin_domain": actor.origin_domain},
                    "answer_id": payload.answer_id,
                },
                channel=channel,
            )
    await session.commit()
    if changed:
        await wake_queued_guild_federation(guild)
        for removed_answer in removed_answers:
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "MESSAGE_POLL_VOTE_REMOVE",
                {
                    "message_id": str(message.id),
                    "message_domain": message.origin_domain,
                    "channel_id": str(channel.id),
                    "channel_domain": channel.origin_domain,
                    "guild_id": str(guild.id),
                    "guild_domain": guild.origin_domain,
                    "user_id": str(actor.id),
                    "user_domain": actor.origin_domain,
                    "answer_id": removed_answer,
                },
            )
        if not payload.remove and inserted is not None:
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "MESSAGE_POLL_VOTE_ADD",
                {
                    "message_id": str(message.id),
                    "message_domain": message.origin_domain,
                    "channel_id": str(channel.id),
                    "channel_domain": channel.origin_domain,
                    "guild_id": str(guild.id),
                    "guild_domain": guild.origin_domain,
                    "user_id": str(actor.id),
                    "user_domain": actor.origin_domain,
                    "answer_id": payload.answer_id,
                },
            )
    return {"voted": not payload.remove}


@router.post("/_kaede/v1/guilds/{guild_id}/proxy-poll-finalize")
async def federation_guild_poll_finalize_proxy(
    guild_id: Snowflake,
    payload: GuildPollFinalizeProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-poll-finalize",
        capacity=120,
        refill_per_minute=120,
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    guild, channel, message, poll = await _federation_poll_context(
        session,
        redis,
        settings,
        int(guild_id),
        actor,
        int(payload.channel_id),
        payload.message_id,
        for_update=True,
    )
    if (message.author_id, message.author_domain) != (actor.id, actor.origin_domain):
        raise HTTPException(status_code=403, detail={"code": "POLL_AUTHOR_REQUIRED"})
    changed = poll.finalized_at is None
    result_message: dict[str, object] | None = None
    result_created = False
    if changed:
        await require_remote_user_creation_allowed(session, actor)
        await require_member_interactions_allowed(
            session,
            guild,
            actor,
            Permission.SEND_POLLS,
        )
        poll.finalized_at = datetime.now(UTC)
        await queue_guild_mutation(
            session,
            settings,
            guild,
            actor,
            "guild.poll.finalize",
            {
                "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                "finalized_at": poll.finalized_at.isoformat(),
            },
            channel=channel,
        )
        result_message, result_created = await ensure_poll_result_message(
            session,
            redis,
            settings,
            snowflake,
            message,
            poll,
        )
    else:
        await session.commit()
    if changed:
        await wake_queued_guild_federation(guild)
    rendered = await render_message_payload(session, message, viewer=actor)
    if changed:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "MESSAGE_UPDATE",
            rendered,
        )
        if result_created and result_message is not None:
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "MESSAGE_CREATE",
                result_message,
            )
    return rendered


@router.post("/_kaede/v1/guilds/{guild_id}/proxy-poll-voters")
async def federation_guild_poll_voters_proxy(
    guild_id: Snowflake,
    payload: GuildPollVotersProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-poll-voters",
        capacity=600,
        refill_per_minute=600,
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    _guild, _channel, message, _poll = await _federation_poll_context(
        session,
        redis,
        settings,
        int(guild_id),
        actor,
        int(payload.channel_id),
        payload.message_id,
        for_update=False,
    )
    answer = await session.get(
        PollAnswer,
        (message.id, message.origin_domain, payload.answer_id),
    )
    if answer is None:
        raise HTTPException(status_code=404, detail={"code": "POLL_ANSWER_NOT_FOUND"})
    conditions = [
        PollVote.message_id == message.id,
        PollVote.message_domain == message.origin_domain,
        PollVote.answer_id == payload.answer_id,
    ]
    if payload.after is not None:
        conditions.append(
            tuple_(PollVote.user_id, PollVote.user_domain) > payload.after.resolve(settings.domain)
        )
    users = list(
        await session.scalars(
            select(User)
            .join(
                PollVote,
                (PollVote.user_id == User.id) & (PollVote.user_domain == User.origin_domain),
            )
            .where(*conditions)
            .order_by(PollVote.user_id, PollVote.user_domain)
            .limit(payload.limit + 1)
        )
    )
    has_more = len(users) > payload.limit
    page = users[: payload.limit]
    return {
        "users": [user_payload(user) for user in page],
        "next_after": (f"{page[-1].id}@{page[-1].origin_domain}" if has_more and page else None),
    }


async def federated_pins_reader(
    payload: ChannelPinsPageProxyRequest,
    principal: FederationPrincipal,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> tuple[User, AuthenticatedUser]:
    """Resolve the authenticated remote reader shared by pin-page proxies."""

    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "channel-pins-page",
        capacity=600,
        refill_per_minute=600,
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    return actor, AuthenticatedUser(
        actor,
        AccessGrant(
            user_id=actor.id,
            user_domain=actor.origin_domain,
            session_id=f"federation:{principal.origin}",
        ),
        "",
        False,
    )


@router.post("/_kaede/v1/guilds/{guild_id}/pins")
async def federation_guild_pins_page(
    guild_id: Snowflake,
    payload: ChannelPinsPageProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return one authoritative pin page to a remote guild member's home."""

    require_guild_federation_access(principal)
    _actor, auth = await federated_pins_reader(
        payload,
        principal,
        session,
        redis,
        settings,
    )
    guild = await home_guild(session, settings, int(guild_id), for_share=True)
    channel = await session.get(Channel, (int(payload.channel_id), guild.origin_domain))
    if (
        channel is None
        or channel.unavailable
        or (channel.guild_id, channel.guild_domain)
        != (
            guild.id,
            guild.origin_domain,
        )
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    return await list_channel_pins(
        EntityRef(f"{channel.id}@{channel.origin_domain}"),
        payload.before,
        payload.limit,
        auth,
        session,
        redis,
        settings,
    )


@router.post("/_kaede/v1/dms/{conversation_id}/pins")
async def federation_dm_pins_page(
    conversation_id: Snowflake,
    payload: ChannelPinsPageProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return one authoritative pin page to a remote DM participant's home."""

    actor, auth = await federated_pins_reader(
        payload,
        principal,
        session,
        redis,
        settings,
    )
    conversation = await session.get(
        DMConversation,
        (int(conversation_id), settings.domain),
    )
    channel = await session.get(Channel, (int(payload.channel_id), settings.domain))
    participant = (
        await session.get(
            DMParticipant,
            (
                int(conversation_id),
                settings.domain,
                actor.id,
                actor.origin_domain,
            ),
        )
        if conversation is not None
        else None
    )
    if (
        conversation is None
        or conversation.authority_domain != settings.domain
        or channel is None
        or channel.unavailable
        or (channel.id, channel.origin_domain) != (conversation.id, conversation.origin_domain)
        or participant is None
    ):
        raise HTTPException(status_code=404, detail={"code": "DM_NOT_FOUND"})
    return await list_channel_pins(
        EntityRef(f"{channel.id}@{channel.origin_domain}"),
        payload.before,
        payload.limit,
        auth,
        session,
        redis,
        settings,
    )


@router.post("/_kaede/v1/guilds/{guild_id}/proxy-pin")
async def federation_guild_pin_proxy(
    guild_id: Snowflake,
    payload: GuildPinProxyRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    """Apply a remote member's pin mutation at the authoritative guild home."""
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-pin-mutation",
        capacity=600,
        refill_per_minute=600,
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    if payload.pinned:
        await require_remote_user_creation_allowed(session, actor)
    guild = await home_guild(session, settings, guild_id, for_update=True)
    channel = await session.get(Channel, (int(payload.channel_id), guild.origin_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.guild_id != guild.id
        or not is_message_capable_channel_type(channel.type, guild_channel=True)
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    pin_access = ChannelAccess(channel=channel, guild=guild, participants=[])
    require_pinnable_channel(pin_access)
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
    if not message_is_pinnable(message):
        raise HTTPException(status_code=400, detail={"code": "SYSTEM_MESSAGE_NOT_PINNABLE"})
    changed = False
    if payload.pinned:
        existing_pin = await session.get(
            Pin,
            (channel.id, channel.origin_domain, message.id, message.origin_domain),
        )
        if existing_pin is None and await channel_pin_count(session, channel) >= CHANNEL_PIN_LIMIT:
            raise HTTPException(status_code=400, detail={"code": "MAXIMUM_PINS_REACHED"})
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
    pin_notice: dict[str, object] | None = None
    pins_update: dict[str, object] | None = None
    if changed:
        signer = await session.get(User, (guild.owner_id, guild.owner_domain))
        if signer is None:
            raise RuntimeError("guild owner disappeared during pin mutation")
        await queue_guild_mutation(
            session,
            settings,
            guild,
            signer,
            "guild.pin.add" if payload.pinned else "guild.pin.remove",
            {
                "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                "channel": {"id": str(channel.id), "origin_domain": channel.origin_domain},
                "user": {"id": str(actor.id), "origin_domain": actor.origin_domain},
            },
            channel=channel,
        )
        if payload.pinned:
            _notice, pin_notice, _destinations = await persist_pin_notice(
                session,
                settings,
                snowflake,
                pin_access,
                actor,
                message,
            )
        await record_pin_audit_entry(
            session,
            snowflake,
            pin_access,
            actor,
            message,
            pinned=payload.pinned,
            reason=payload.reason,
        )
        pins_update = await channel_pins_update_payload(
            session,
            channel,
            guild,
            changed_message=message,
            pinned=payload.pinned,
        )
    await session.commit()
    if changed:
        await wake_queued_guild_federation(guild)
        if pins_update is None:
            raise RuntimeError("pin mutation lost its gateway projection")
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "CHANNEL_PINS_UPDATE",
            pins_update,
        )
        if pin_notice is not None:
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "MESSAGE_CREATE",
                pin_notice,
            )
    return {"pinned": payload.pinned}


@router.post("/_kaede/v1/guilds/{guild_id}/message-operation")
async def federation_guild_message_operation(
    guild_id: Snowflake,
    payload: GuildMessageOperationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Apply a typed message operation at the authoritative guild home."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-message-operation",
        capacity=3_000,
        refill_per_minute=3_000,
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    guild = await home_guild(session, settings, guild_id, for_update=False)
    channel = await session.get(Channel, (int(payload.channel_id), guild.origin_domain))
    if (
        channel is None
        or channel.unavailable
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    requires_mutation_admission = payload.operation != "message.delete"
    if not requires_mutation_admission:
        if payload.message_id is None:
            raise RuntimeError("validated delete operation lost its message")
        delete_ref = payload.message_id.resolve(settings.domain)
        delete_target = await session.get(Message, delete_ref)
        requires_mutation_admission = (
            delete_target is None
            or (delete_target.channel_id, delete_target.channel_domain)
            != (channel.id, channel.origin_domain)
            or (delete_target.author_id, delete_target.author_domain)
            != (actor.id, actor.origin_domain)
        )
    if requires_mutation_admission:
        await require_remote_user_creation_allowed(session, actor)
    auth = AuthenticatedUser(
        actor,
        AccessGrant(
            user_id=actor.id,
            user_domain=actor.origin_domain,
            session_id=f"federation:{principal.origin}",
        ),
        "",
        False,
    )
    channel_ref = EntityRef(f"{channel.id}@{channel.origin_domain}")
    message_ref = payload.message_id
    if payload.operation == "message.edit":
        if message_ref is None or payload.edit is None:
            raise RuntimeError("validated edit operation lost its body")
        application_ref = await validated_proxy_application(
            session,
            guild,
            actor,
            payload.application_id,
        )
        message_id, message_domain = message_ref.resolve(settings.domain)
        try:
            edit_expression_tokens = expression_custom_emoji_tokens(
                content=(
                    payload.edit.content if "content" in payload.edit.model_fields_set else None
                ),
                components=(
                    payload.edit.components
                    if "components" in payload.edit.model_fields_set
                    else None
                ),
                poll=None,
                e2ee=(payload.edit.e2ee if "e2ee" in payload.edit.model_fields_set else None),
                default_domain=guild.origin_domain,
            )
            operation_id = hashlib.sha256(
                f"message.edit\n{message_id}@{message_domain}".encode()
            ).hexdigest()
            attested_tokens, attested_items = await validate_expression_authorization_map(
                session,
                redis,
                settings,
                payload.expression_authorizations,
                requester_ref=f"{actor.id}@{actor.origin_domain}",
                requester_type=cast(Literal["human", "bot"], actor.account_type),
                application_ref=(
                    f"{application_ref[0]}@{application_ref[1]}"
                    if application_ref is not None
                    else None
                ),
                target_guild_ref=f"{guild.id}@{guild.origin_domain}",
                target_channel_ref=f"{channel.id}@{channel.origin_domain}",
                target_message_ref=f"{message_id}@{message_domain}",
                operation="message.edit",
                operation_id=operation_id,
                emoji_tokens=edit_expression_tokens,
                sticker_items=payload.expression_sticker_items,
            )
            await validate_attested_expression_target(
                session,
                actor,
                guild,
                await require_permissions(
                    session,
                    redis,
                    guild,
                    actor,
                    required_permissions("message.edit.self"),
                    channel=channel,
                ),
                attested_tokens,
                attested_items,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            ) from exc
        proxy_installation = await require_proxy_bot_e2ee_participation(
            session,
            guild,
            channel,
            actor,
            application_ref,
            payload.edit.e2ee,
        )
        authoritative_mentions = tuple(
            item.resolve(actor.origin_domain) for item in payload.authoritative_mention_user_ids
        )
        rendered = await edit_message(
            channel_ref,
            message_ref,
            payload.edit,
            auth,
            session,
            redis,
            settings,
            snowflake,
            MessageMutationOptions(
                application_id=(application_ref[0] if application_ref is not None else None),
                application_domain=(application_ref[1] if application_ref is not None else None),
                bot_installation_id=(
                    proxy_installation.id if proxy_installation is not None else None
                ),
                expression_authorization_checked=True,
                attested_expression_tokens=tuple(attested_tokens),
                attested_expression_sticker_items=tuple(attested_items),
                authoritative_mention_refs=(
                    authoritative_mentions if payload.authoritative_mention_user_ids else None
                ),
                authoritative_attachment_refs=(
                    tuple(item.resolve(settings.domain) for item in payload.attachment_refs)
                    if payload.edit.attachment_ids is not None
                    else None
                ),
                replicated_attachments=tuple(payload.attachments),
            ),
        )
        return {"message": rendered}
    if payload.operation == "message.delete":
        if message_ref is None:
            raise RuntimeError("validated delete operation lost its message")
        await delete_message(channel_ref, message_ref, auth, session, redis, settings)
        return {"deleted": True}
    if payload.operation == "message.bulk_delete":
        await bulk_delete_messages(
            channel_ref,
            MessageBulkDelete(message_ids=payload.message_ids),
            auth,
            session,
            redis,
            settings,
        )
        return {"deleted": True}
    if payload.operation == "reaction.remove_user":
        if message_ref is None or payload.target_user_id is None or payload.emoji is None:
            raise RuntimeError("validated reaction operation lost required fields")
        await remove_user_reaction(
            channel_ref,
            message_ref,
            payload.target_user_id,
            payload.emoji,
            auth,
            session,
            redis,
            settings,
        )
        return {"removed": True}
    if payload.operation == "reaction.clear":
        if message_ref is None:
            raise RuntimeError("validated reaction clear lost its message")
        await _clear_reactions(
            channel_ref,
            message_ref,
            payload.emoji,
            auth,
            session,
            redis,
            settings,
        )
        return {"removed": True}
    if payload.operation == "announcement.crosspost":
        if message_ref is None:
            raise RuntimeError("validated crosspost lost its message")
        rendered = await crosspost_message(
            channel_ref,
            message_ref,
            auth,
            session,
            redis,
            snowflake,
            settings,
        )
        return {"message": rendered}
    raise HTTPException(status_code=422, detail={"code": "MESSAGE_OPERATION_INVALID"})


@router.post("/_kaede/v1/dms/{conversation_id}/message-operation")
async def federation_dm_message_operation(
    conversation_id: Snowflake,
    payload: DMMessageOperationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Apply a typed DM message operation at its conversation authority."""

    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "dm-message-operation",
        capacity=3_000,
        refill_per_minute=3_000,
    )
    if payload.actor.domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    conversation = await session.get(
        DMConversation,
        (int(conversation_id), settings.domain),
    )
    if conversation is None or conversation.authority_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "DM_NOT_FOUND"})
    actor = await session.get(User, (int(payload.actor.id), payload.actor.domain))
    if (
        actor is None
        or await session.get(
            DMParticipant,
            (
                conversation.id,
                conversation.origin_domain,
                int(payload.actor.id),
                payload.actor.domain,
            ),
        )
        is None
    ):
        raise HTTPException(status_code=403, detail={"code": "DM_ACCESS_DENIED"})
    if payload.operation in {
        "message.edit",
        "reaction.add",
        "poll.vote.add",
        "poll.end",
        "pin.add",
    }:
        await require_remote_user_creation_allowed(session, actor)
    auth = AuthenticatedUser(
        actor,
        AccessGrant(
            user_id=actor.id,
            user_domain=actor.origin_domain,
            session_id=f"federation:{principal.origin}",
        ),
        "",
        False,
    )
    channel_ref = EntityRef(f"{conversation.id}@{conversation.origin_domain}")
    message_ref = payload.message_id
    if payload.operation == "message.edit":
        if payload.edit is None:
            raise RuntimeError("validated edit operation lost its body")
        application_ref: tuple[int, str] | None = None
        if payload.application_id is not None:
            application_ref = payload.application_id.resolve(actor.origin_domain)
            application = await session.get(BotApplication, application_ref)
            if (
                application is None
                or application.status != "active"
                or (application.bot_user_id, application.bot_user_domain)
                != (actor.id, actor.origin_domain)
            ):
                raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
        authoritative_mentions = tuple(
            item.resolve(actor.origin_domain) for item in payload.authoritative_mention_user_ids
        )
        rendered = await edit_message(
            channel_ref,
            message_ref,
            payload.edit,
            auth,
            session,
            redis,
            settings,
            snowflake,
            MessageMutationOptions(
                application_id=(application_ref[0] if application_ref is not None else None),
                application_domain=(application_ref[1] if application_ref is not None else None),
                trusted_external_domain=actor.origin_domain,
                authoritative_mention_refs=(
                    authoritative_mentions if payload.authoritative_mention_user_ids else None
                ),
                authoritative_attachment_refs=(
                    tuple(item.resolve(settings.domain) for item in payload.attachment_refs)
                    if payload.edit.attachment_ids is not None
                    else None
                ),
                replicated_attachments=tuple(payload.attachments),
            ),
        )
        return {"message": rendered}
    if payload.operation == "message.delete":
        await delete_message(channel_ref, message_ref, auth, session, redis, settings)
        return {"deleted": True}
    if payload.operation == "reaction.add":
        if payload.emoji is None:
            raise RuntimeError("validated reaction operation lost its emoji")
        await add_reaction(
            channel_ref,
            message_ref,
            ReactionCreate(emoji=payload.emoji),
            Response(),
            auth,
            session,
            redis,
            settings,
        )
        return {"updated": True}
    if payload.operation == "reaction.remove":
        if payload.emoji is None:
            raise RuntimeError("validated reaction operation lost its emoji")
        await remove_own_reaction(
            channel_ref,
            message_ref,
            Response(),
            payload.emoji,
            auth,
            session,
            redis,
            settings,
        )
        return {"updated": True}
    if payload.operation == "reaction.list":
        if payload.emoji is None:
            raise RuntimeError("validated reaction operation lost its emoji")
        return await list_reaction_users(
            channel_ref,
            message_ref,
            payload.emoji,
            payload.after,
            payload.limit,
            auth,
            session,
            redis,
            settings,
        )
    if payload.operation in {"poll.vote.add", "poll.vote.remove"}:
        if payload.answer_id is None:
            raise RuntimeError("validated poll vote operation lost its answer")
        operation = add_poll_vote if payload.operation.endswith("add") else remove_poll_vote
        await operation(
            channel_ref,
            message_ref,
            payload.answer_id,
            auth,
            session,
            redis,
            settings,
        )
        return {"updated": True}
    if payload.operation == "poll.voters.list":
        if payload.answer_id is None:
            raise RuntimeError("validated poll voters operation lost its answer")
        return await list_poll_voters(
            channel_ref,
            message_ref,
            payload.answer_id,
            payload.after,
            payload.limit,
            auth,
            session,
            redis,
            settings,
        )
    if payload.operation == "poll.end":
        rendered = await finalize_poll(
            channel_ref,
            message_ref,
            auth,
            session,
            redis,
            snowflake,
            settings,
        )
        return {"message": rendered}
    if payload.operation in {"pin.add", "pin.remove"}:
        if payload.operation.endswith("add"):
            await pin_message(
                channel_ref,
                message_ref,
                auth,
                session,
                redis,
                settings,
                snowflake,
                None,
            )
        else:
            await unpin_message(
                channel_ref,
                message_ref,
                auth,
                session,
                redis,
                settings,
                snowflake,
                None,
            )
        return {"updated": True}
    raise HTTPException(status_code=422, detail={"code": "MESSAGE_OPERATION_INVALID"})


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
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-reaction-mutation",
        capacity=3_000,
        refill_per_minute=3_000,
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    if not payload.remove:
        await require_remote_user_creation_allowed(session, actor)
    guild = await home_guild(session, settings, guild_id, for_update=True)
    channel = await session.get(Channel, (int(payload.channel_id), guild.origin_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.guild_id != guild.id
        or not is_message_capable_channel_type(channel.type, guild_channel=True)
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
    if not payload.remove:
        await require_member_interactions_allowed(
            session,
            guild,
            actor,
            Permission.ADD_REACTIONS,
        )
    message_id, message_domain = payload.message_id.resolve(settings.domain)
    if payload.remove:
        if payload.expression_authorizations or payload.application_id is not None:
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            )
    else:
        expression_tokens = [payload.emoji] if custom_emoji_refs(payload.emoji) else []
        operation_id = hashlib.sha256(
            f"reaction.add\n{message_id}@{message_domain}\n{payload.emoji}".encode()
        ).hexdigest()
        application_ref = await validated_proxy_application(
            session,
            guild,
            actor,
            payload.application_id,
            required_scope="reactions.write",
        )
        try:
            attested_tokens, attested_items = await validate_expression_authorization_map(
                session,
                redis,
                settings,
                payload.expression_authorizations,
                requester_ref=f"{actor.id}@{actor.origin_domain}",
                requester_type=cast(Literal["human", "bot"], actor.account_type),
                application_ref=(
                    f"{application_ref[0]}@{application_ref[1]}"
                    if application_ref is not None
                    else None
                ),
                target_guild_ref=f"{guild.id}@{guild.origin_domain}",
                target_channel_ref=f"{channel.id}@{channel.origin_domain}",
                target_message_ref=f"{message_id}@{message_domain}",
                operation="reaction.add",
                operation_id=operation_id,
                emoji_tokens=expression_tokens,
                sticker_items=[],
            )
            await validate_attested_expression_target(
                session,
                actor,
                guild,
                actor_permissions,
                attested_tokens,
                attested_items,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            ) from exc
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
        await queue_guild_mutation(
            session,
            settings,
            guild,
            actor,
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
            "MESSAGE_REACTION_REMOVE" if payload.remove else "MESSAGE_REACTION_ADD",
            reaction_event_payload(
                message_id=message.id,
                message_domain=message.origin_domain,
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                user_id=actor.id,
                user_domain=actor.origin_domain,
                emoji=payload.emoji,
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                message_author_id=message.author_id,
                message_author_domain=message.author_domain,
                removed=payload.remove,
            ),
        )
    return {"reacted": not payload.remove}
