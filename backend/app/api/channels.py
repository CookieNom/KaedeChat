from __future__ import annotations

import hashlib
import secrets
from collections.abc import Awaitable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

import httpx
import structlog
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from redis.asyncio import Redis
from sqlalchemy import case, delete, exists, func, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import (
    AuthenticatedUser,
    federated_authenticated_user,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.automod.service import (
    AutoModPostCommit,
    require_member_interactions_allowed,
)
from app.automod.service import (
    evaluate_message as evaluate_automod_message,
)
from app.bots.e2ee import active_bot_e2ee_participation, require_bot_e2ee_participation
from app.bots.installations import usable_guild_installation, user_installation_is_usable
from app.chat.allowed_mentions import (
    EVERYONE_MENTION,
    ResolvedMentions,
    allowed_mention_texts,
    everyone_mention_recipients,
    resolve_allowed_mentions_projection,
    selected_allowed_mentions,
)
from app.chat.announcement_identity import (
    federated_crosspost_key,
    federated_follow_key,
    qualified_follow_ref,
)
from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.channel_access import (
    ChannelAccess,
    effective_channel_nsfw,
    load_channel_access,
    lock_local_channel_mutation,
    publish_channel_dispatch,
)
from app.chat.custom_emojis import (
    canonical_reaction_emoji,
    custom_emoji_refs,
    resolve_rich_custom_emojis,
    validate_custom_emoji_tokens,
    validate_custom_emoji_use,
)
from app.chat.custom_stickers import (
    custom_sticker_refs,
    resolve_sticker_items,
    validate_custom_sticker_use,
)
from app.chat.dm_mutations import DM_MESSAGE_MUTATION_EVENTS
from app.chat.e2ee import (
    MessageEncryptionPolicyError,
    interaction_routing_poll,
    validate_e2ee_message_revision,
    validate_interaction_routing_contract,
    validate_message_encryption_policy,
)
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.expression_authorization import expression_custom_emoji_tokens
from app.chat.forwarding import (
    FORWARD_SOURCE_AUTHORIZATION_EVENT,
    FORWARDABLE_MESSAGE_TYPES,
    build_forward_source_authorization_content,
    can_forward_between_age_contexts,
    forward_snapshot_custom_emoji_tokens,
    forward_snapshot_matches_attachments,
    forward_snapshot_sticker_items,
    rebind_forward_snapshot_attachments,
    validate_forward_snapshot_source_binding,
)
from app.chat.guild_revision import (
    build_guild_authority_envelope,
    federation_channel_state,
    guild_authority_owner,
    guild_mutation_signer,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.interaction_metadata import validate_interaction_metadata
from app.chat.mention_policy import regular_message_allowed_mentions
from app.chat.mentions import (
    merge_mention_recipients,
    role_mention_recipients,
    role_mention_refs,
)
from app.chat.message_flags import (
    MESSAGE_FLAG_CROSSPOSTED,
    MESSAGE_FLAG_HAS_SNAPSHOT,
    MESSAGE_FLAG_IS_COMPONENTS_V2,
    MESSAGE_FLAG_IS_CROSSPOST,
    MESSAGE_FLAG_IS_VOICE_MESSAGE,
    MESSAGE_FLAG_SOURCE_MESSAGE_DELETED,
    MESSAGE_FLAG_SUPPRESS_EMBEDS,
    MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS,
    inferred_message_shape_flags,
)
from app.chat.message_references import build_qualified_message_reference
from app.chat.payloads import (
    attachment_payload,
    channel_payload,
    dm_channel_payload,
    guild_payload,
    message_payload,
    render_message_payload,
    render_poll_payload,
    rich_thread_member_payload,
    thread_member_payload,
    thread_source_starter_payload,
    user_payload,
)
from app.chat.permissions import get_permissions, require_permissions
from app.chat.pins import (
    CHANNEL_PIN_LIMIT,
    PIN_NOTICE_MESSAGE_TYPE,
    channel_pin_count,
    channel_pins_update_payload,
    message_is_pinnable,
    normalize_pin_cursor,
    validate_pin_page_payload,
)
from app.chat.poll_results import (
    POLL_RESULT_MESSAGE_TYPE,
    build_poll_result_projection,
    poll_result_embed,
    validate_poll_result_projection,
)
from app.chat.postcommit import queue_postcommit_dispatch
from app.chat.privacy import blocked_between, lock_relationship_pair, require_can_direct_message
from app.chat.reaction_payloads import (
    reaction_emoji_payload,
    reaction_event_payload,
    reaction_payloads_for_messages,
)
from app.chat.rich_content import (
    message_automod_text,
    uses_components_v2,
    validate_attachment_url_references,
)
from app.chat.schemas import (
    ChannelFollowCreate,
    MessageBulkDelete,
    MessageCreate,
    MessageEdit,
    MessageForwardCreate,
    MessageForwardPrepare,
    ReactionCreate,
    ReadStateUpdate,
    parse_actor_intent_headers,
)
from app.chat.thread_limits import require_active_thread_capacity
from app.chat.voice_messages import require_voice_message_guild_capacity
from app.chat.webhook_limits import (
    lock_webhook_capacity_guild,
    require_webhook_capacity,
)
from app.core.channel_types import (
    is_message_capable_channel_type,
    is_pinnable_guild_channel_type,
)
from app.core.errors import parse_upstream_error
from app.core.permission_contract import required_permissions
from app.core.permissions import ALL_PERMISSIONS, Permission
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import MAX_SNOWFLAKE, EntityRef, EntityReferenceLike, validate_snowflake
from app.db.bot_models import (
    BotApplication,
    BotDMCapability,
    BotInstallation,
    BotUserInstallation,
)
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Attachment,
    Channel,
    ChannelFollow,
    DMConversation,
    DMParticipant,
    E2EEDevice,
    EncryptedForumStarterReservation,
    FederatedChannelFollow,
    FederatedMessageCrosspost,
    FederationEvent,
    FederationOutbox,
    Guild,
    GuildMember,
    MediaTombstoneSource,
    Message,
    MessageCrosspost,
    MessageProjection,
    MessageView,
    Pin,
    Poll,
    PollAnswer,
    PollVote,
    Reaction,
    ReadState,
    TerminalRoomDeletion,
    ThreadMember,
    User,
    Webhook,
)
from app.federation.actor_intents import actor_intent_for_authority
from app.federation.client import signed_request
from app.federation.dm_history import (
    MAX_DM_HISTORY_RESPONSE_BYTES,
    dm_history_page_is_complete,
    merge_dm_history_messages,
    validate_dm_history_page,
)
from app.federation.dm_storage import (
    FederatedDMQuotaExceeded,
    admit_federated_dm_message,
    dm_authority_history_available,
    dm_history_metadata,
    dm_message_storage_delta,
    lock_federated_dm_authority,
    opaque_dm_history_ref_allowed,
)
from app.federation.events import (
    build_envelope,
    message_attachment_refs,
    queue_event,
    record_attachment_recipients,
)
from app.federation.expression_authorization import (
    acquire_expression_use_authorizations,
    validate_attested_expression_target,
    validate_expression_authorization_map,
)
from app.federation.forwarding import validated_forward_source_proof
from app.federation.guild_media_deletions import queue_guild_media_delete_request
from app.federation.guilds import (
    GuildSequenceGap,
    apply_guild_message_event,
    assign_guild_sequence,
    remote_destinations_with_channel_access,
    store_guild_event,
)
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.replica_storage import (
    REPLICA_QUOTA_ERROR_CODE,
    FederationReplicaQuotaExceeded,
    admit_replica_storage,
    mark_replica_quota_paused,
)
from app.federation.replication import profile_from_user, replicate_message_attachments
from app.federation.security import validated_event_envelope
from app.federation.terminal_rooms import lock_terminal_room
from app.federation.typing import (
    TypingPublishRequest,
    new_typing_projection,
    publish_authoritative_typing,
)
from app.media.payloads import federation_attachment_payload
from app.media.service import attachments_for_messages, discard_attachment, finalize_attachment
from app.media.tombstones import lock_media_tombstone_ref, queue_terminal_attachment_tombstone
from app.tasks import (
    SET_LATEST_MESSAGE_SCRIPT,
    announcement_crosspost_deliver,
    federation_deliver,
    federation_guild_sync,
    media_local_purge,
    media_process,
    mentions_fanout,
)

router = APIRouter(prefix="/api/v1/channels", tags=["messages"])

CHANNEL_FOLLOW_ADD_MESSAGE_TYPE = 12
ANNOUNCEMENT_FOLLOW_AUTHORIZATION_TTL = timedelta(minutes=5)


async def require_owned_e2ee_sender_device(
    session: AsyncSession,
    user: User,
    envelope: object,
    *,
    authority_domain: str,
    channel: Channel | None = None,
    bot_installation_id: int | None = None,
    bot_user_installation_id: int | None = None,
    bot_dm_capability_id: int | None = None,
    bot_worker_id: int | None = None,
    webhook_id: int | None = None,
    webhook_domain: str | None = None,
    webhook_e2ee_device_id: str | None = None,
) -> None:
    if not isinstance(envelope, dict):
        raise HTTPException(status_code=409, detail={"code": "E2EE_SENDER_DEVICE_INVALID"})
    device_id = envelope.get("sender_device_id")
    if webhook_id is not None:
        from app.api.webhook_e2ee import require_webhook_e2ee_participation

        webhook = await session.get(Webhook, webhook_id)
        if (
            webhook is None
            or webhook.revoked_at is not None
            or webhook.guild_domain != webhook_domain
            or channel is None
            or not isinstance(device_id, str)
            or device_id != webhook_e2ee_device_id
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "WEBHOOK_E2EE_PARTICIPANT_REQUIRED"},
            )
        await require_webhook_e2ee_participation(
            session,
            webhook,
            channel,
            device_id,
        )
        return
    if getattr(user, "account_type", "human") == "bot":
        if (
            sum(
                value is not None
                for value in (
                    bot_installation_id,
                    bot_user_installation_id,
                    bot_dm_capability_id,
                )
            )
            > 1
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"},
            )
        installation: BotInstallation | BotUserInstallation | BotDMCapability | None
        if bot_installation_id is not None:
            installation = await session.get(BotInstallation, bot_installation_id)
        elif bot_user_installation_id is not None:
            installation = await session.get(BotUserInstallation, bot_user_installation_id)
        elif bot_dm_capability_id is not None:
            installation = await session.get(BotDMCapability, bot_dm_capability_id)
        else:
            installation = None
        if (
            installation is None
            or (
                isinstance(installation, BotInstallation)
                and (installation.bot_user_id, installation.bot_user_domain)
                != (user.id, user.origin_domain)
            )
            or (
                isinstance(installation, BotDMCapability)
                and (
                    (installation.bot_user_id, installation.bot_user_domain)
                    != (user.id, user.origin_domain)
                    or (installation.conversation_id, installation.conversation_domain)
                    != (getattr(channel, "id", None), getattr(channel, "origin_domain", None))
                    or installation.revoked_at is not None
                    or installation.expires_at <= datetime.now(UTC)
                )
            )
            or (
                isinstance(installation, BotUserInstallation)
                and not user_installation_is_usable(
                    installation,
                    current_instance_domain=authority_domain,
                )
            )
            or (
                not isinstance(installation, BotUserInstallation)
                and installation.status != "active"
            )
            or not isinstance(device_id, str)
            or channel is None
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"},
            )
        await require_bot_e2ee_participation(
            session,
            installation,
            channel,
            device_id,
            worker_id=bot_worker_id,
        )
        return
    device = await session.get(E2EEDevice, device_id) if isinstance(device_id, str) else None
    if (
        device is None
        or (device.user_id, device.user_domain) != (user.id, user.origin_domain)
        or device.revoked_at is not None
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_SENDER_DEVICE_INVALID"})


DM_REACTIONS_PER_MESSAGE_LIMIT = 100
THREAD_CHANNEL_TYPES = frozenset({10, 11, 12})
MESSAGE_FLAG_FAILED_TO_MENTION_SOME_ROLES_IN_THREAD = 1 << 8


def advance_thread_message_projection(channel: Channel, message: Message) -> None:
    """Advance a thread cursor/activity projection without snowflake regression."""

    current_cursor = (
        (channel.last_message_id, channel.last_message_domain)
        if channel.last_message_id is not None and channel.last_message_domain is not None
        else None
    )
    message_cursor = (message.id, message.origin_domain)
    if current_cursor is None or current_cursor < message_cursor:
        channel.last_message_id, channel.last_message_domain = message_cursor
    if channel.last_activity_at is None or channel.last_activity_at < message.created_at:
        channel.last_activity_at = message.created_at


def encrypted_rich_routing(
    envelope: object,
) -> tuple[dict[str, object] | None, list[dict[str, object]], dict[str, object] | None]:
    """Return authenticated public routing metadata without exposing rich text."""

    if not isinstance(envelope, dict) or "rich_payload_digest" not in envelope:
        return None, [], None
    raw_contract = envelope.get("interaction_contract")
    if raw_contract is None:
        return None, [], None
    contract = validate_interaction_routing_contract(raw_contract, callback_type=None)
    raw_controls = contract.get("components", [])
    if not isinstance(raw_controls, list):
        raise ValueError("encrypted message routing controls are invalid")
    controls = [
        {str(key): value for key, value in item.items()}
        for item in raw_controls
        if isinstance(item, dict)
    ]
    return contract, controls, interaction_routing_poll(contract)


def require_encrypted_rich_admission(
    envelope: object,
    *,
    author: User,
    attachments: Sequence[Attachment | dict[str, object]],
    mention_refs: Sequence[tuple[int, str]],
    sticker_items: Sequence[dict[str, object]],
    referenced_message_ref: tuple[int, str] | None,
    application_ref: tuple[int, str] | None,
    installation_lineage: tuple[str, int, str, int] | None,
    has_controls: bool,
    tts: bool,
    voice_message: bool,
    flags: int,
    view_persistent: bool,
    view_version: int,
    forwarded_message_ref: tuple[int, str] | None,
    forwarded_channel_ref: tuple[int, str] | None,
    forward_source_projection_digest: str | None,
    forwarded_created_at: datetime | None,
    forwarded_edited_at: datetime | None,
    forwarded_flags: int | None,
    forwarded_message_type: int | None,
) -> None:
    """Bind a rich MLS envelope to the exact authority-admitted projection."""

    if not isinstance(envelope, dict) or "rich_payload_digest" not in envelope:
        return
    attachment_refs = sorted(
        f"{item.id}@{item.origin_domain}"
        if isinstance(item, Attachment)
        else f"{item.get('id')}@{item.get('origin_domain')}"
        for item in attachments
    )
    expected_mentions = sorted(f"{user_id}@{domain}" for user_id, domain in mention_refs)
    expected_stickers = sorted(
        f"{item.get('id')}@{item.get('origin_domain')}" for item in sticker_items
    )
    expected_reference = (
        f"{referenced_message_ref[0]}@{referenced_message_ref[1]}"
        if referenced_message_ref is not None
        else None
    )
    expected_application = (
        f"{application_ref[0]}@{application_ref[1]}" if application_ref is not None else None
    )
    expected_installation = (
        f"{installation_lineage[1]}@{installation_lineage[2]}"
        if installation_lineage is not None
        else None
    )
    expected_integration = installation_lineage[0] if installation_lineage is not None else None
    expected_revision = str(installation_lineage[3]) if installation_lineage is not None else None
    expected_forwarded_message = (
        f"{forwarded_message_ref[0]}@{forwarded_message_ref[1]}"
        if forwarded_message_ref is not None
        else None
    )
    expected_forwarded_channel = (
        f"{forwarded_channel_ref[0]}@{forwarded_channel_ref[1]}"
        if forwarded_channel_ref is not None
        else None
    )
    if (
        envelope.get("author_ref") != f"{author.id}@{author.origin_domain}"
        or envelope.get("message_attachment_refs") != attachment_refs
        or envelope.get("message_mention_refs") != expected_mentions
        or envelope.get("message_sticker_refs") != expected_stickers
        or envelope.get("referenced_message_ref") != expected_reference
        or envelope.get("application_ref") != expected_application
        or envelope.get("interaction_integration_type") != expected_integration
        or envelope.get("interaction_installation_ref") != expected_installation
        or envelope.get("interaction_installation_revision") != expected_revision
        or envelope.get("view_version") != str(view_version)
        or envelope.get("view_persistent") is not view_persistent
        or envelope.get("tts") is not tts
        or envelope.get("voice_message") is not voice_message
        or envelope.get("message_flags") != flags
        or envelope.get("forwarded_message_ref") != expected_forwarded_message
        or envelope.get("forwarded_channel_ref") != expected_forwarded_channel
        or envelope.get("forward_source_projection_digest") != forward_source_projection_digest
        or envelope.get("forwarded_created_at")
        != (forwarded_created_at.isoformat() if forwarded_created_at is not None else None)
        or envelope.get("forwarded_edited_at")
        != (forwarded_edited_at.isoformat() if forwarded_edited_at is not None else None)
        or envelope.get("forwarded_flags") != forwarded_flags
        or envelope.get("forwarded_message_type") != forwarded_message_type
        or (envelope.get("forward_snapshot_digest") is not None)
        != (forwarded_message_ref is not None)
        or has_controls
        and view_version < 1
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
        )


def require_reencrypted_forward_attachments(
    source_count: int,
    destination: Sequence[Attachment | dict[str, object]],
) -> None:
    """Require one fresh destination ciphertext per source attachment."""

    if source_count != len(destination):
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_FORWARD_ATTACHMENT_MISMATCH"},
        )


def _forward_proof_http_error(code: str = "FORWARD_SOURCE_PROOF_INVALID") -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code})


async def validate_signed_forward_source_proof(
    session: AsyncSession,
    settings: Settings,
    raw_proof: object,
    *,
    requester: User,
    source_message_ref: tuple[int, str],
    source_channel_ref: tuple[int, str],
    destination_channel: Channel,
    nonce: str,
    application_ref: tuple[int, str] | None,
    e2ee_device_id: str | None,
    validation_time: datetime | None = None,
) -> dict[str, object]:
    """Verify signature and every use-site binding before trusting a proof."""

    try:
        return await validated_forward_source_proof(
            session,
            settings,
            raw_proof,
            requester_ref=f"{requester.id}@{requester.origin_domain}",
            requester_type=cast(Literal["human", "bot"], requester.account_type),
            source_message_ref=f"{source_message_ref[0]}@{source_message_ref[1]}",
            source_channel_ref=f"{source_channel_ref[0]}@{source_channel_ref[1]}",
            destination_channel_ref=f"{destination_channel.id}@{destination_channel.origin_domain}",
            destination_encryption_mode=cast(
                Literal["plaintext", "e2ee"], destination_channel.encryption_mode
            ),
            nonce=nonce,
            application_ref=(
                f"{application_ref[0]}@{application_ref[1]}"
                if application_ref is not None
                else None
            ),
            e2ee_device_id=e2ee_device_id,
            validation_time=validation_time,
        )
    except (TypeError, ValueError) as exc:
        raise _forward_proof_http_error() from exc


async def local_forward_source_proof(
    session: AsyncSession,
    settings: Settings,
    *,
    requester: User,
    source: Message,
    source_channel: Channel,
    destination_channel: Channel,
    attachments: list[Attachment],
    source_nsfw: bool,
    nonce: str,
    application_ref: tuple[int, str] | None,
    e2ee_device_id: str | None,
) -> dict[str, object]:
    """Build the same signed contract without an unnecessary self HTTP hop."""

    try:
        content = build_forward_source_authorization_content(
            source,
            attachments,
            requester_ref=f"{requester.id}@{requester.origin_domain}",
            requester_type=cast(Literal["human", "bot"], requester.account_type),
            source_channel_ref=f"{source_channel.id}@{source_channel.origin_domain}",
            destination_channel_ref=(
                f"{destination_channel.id}@{destination_channel.origin_domain}"
            ),
            destination_encryption_mode=cast(
                Literal["plaintext", "e2ee"], destination_channel.encryption_mode
            ),
            source_nsfw=source_nsfw,
            nonce=nonce,
            application_ref=(
                f"{application_ref[0]}@{application_ref[1]}"
                if application_ref is not None
                else None
            ),
            e2ee_device_id=e2ee_device_id,
        )
    except ValueError as exc:
        raise _forward_proof_http_error("FORWARD_SOURCE_PROOF_UNAVAILABLE") from exc
    return await build_envelope(
        session,
        settings,
        FORWARD_SOURCE_AUTHORIZATION_EVENT,
        requester,
        content,
        context={"source_channel_ref": f"{source_channel.id}@{source_channel.origin_domain}"},
        authority_attested_actor=requester.origin_domain != settings.domain,
    )


async def remote_forward_source_proof(
    session: AsyncSession,
    settings: Settings,
    *,
    requester: User,
    source_message_ref: tuple[int, str],
    source_channel: Channel,
    destination_channel: Channel,
    nonce: str,
) -> dict[str, object]:
    """Acquire a human proof directly from the exact source authority."""

    if requester.account_type != "human":
        raise _forward_proof_http_error("BOT_FORWARD_SOURCE_PROOF_REQUIRED")
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            source_channel.origin_domain,
            f"/_kaede/v1/channels/{source_channel.id}/forward-authorize",
            payload={
                "actor": profile_from_user(requester),
                "source_message_ref": (f"{source_message_ref[0]}@{source_message_ref[1]}"),
                "destination_channel_ref": (
                    f"{destination_channel.id}@{destination_channel.origin_domain}"
                ),
                "destination_encryption_mode": destination_channel.encryption_mode,
                "nonce": nonce,
            },
            request_timeout=10,
            max_response_bytes=1024 * 1024,
            guild_context=source_channel.guild_id is not None,
        )
    except FederationNetworkError:
        raise HTTPException(
            status_code=503,
            detail={"code": "FORWARD_SOURCE_AUTHORITY_UNAVAILABLE"},
        ) from None
    if response.status_code in {400, 403, 404, 409, 429}:
        raise_proxy_rejection(response, {400, 403, 404, 409, 429})
    if response.status_code != 200:
        raise HTTPException(
            status_code=503,
            detail={"code": "FORWARD_SOURCE_AUTHORITY_UNAVAILABLE"},
        )
    try:
        body = decode_federation_response_json(response, max_response_bytes=1024 * 1024)
    except FederationNetworkError:
        raise _forward_proof_http_error() from None
    raw_proof = body.get("authorization") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or set(body) != {"authorization"}
        or not isinstance(raw_proof, dict)
    ):
        raise _forward_proof_http_error()
    return {str(key): value for key, value in raw_proof.items()}


def add_encrypted_poll_rows(
    session: AsyncSession,
    message: Message,
    contract: dict[str, object],
) -> None:
    """Materialize only label-free poll state for voting and federation."""

    session.add(
        Poll(
            message_id=message.id,
            message_domain=message.origin_domain,
            question={"encrypted": True, "version": 1},
            allow_multiselect=cast(bool, contract["allow_multiselect"]),
            layout_type=1,
            expires_at=datetime.now(UTC)
            + timedelta(seconds=cast(int, contract["duration_seconds"])),
        )
    )
    for answer_id in cast(list[int], contract["answer_ids"]):
        session.add(
            PollAnswer(
                message_id=message.id,
                message_domain=message.origin_domain,
                answer_id=answer_id,
                # The legacy table requires one non-null media field. This
                # marker is never projected; labels and emoji remain inside MLS.
                text=f"encrypted:{answer_id}",
                emoji=None,
            )
        )


async def require_editable_message(session: AsyncSession, message: Message) -> None:
    """Apply message-kind immutability consistently to every edit surface."""

    if message.message_type in {
        PIN_NOTICE_MESSAGE_TYPE,
        CHANNEL_FOLLOW_ADD_MESSAGE_TYPE,
        POLL_RESULT_MESSAGE_TYPE,
    }:
        raise HTTPException(
            status_code=400,
            detail={"code": "SYSTEM_MESSAGE_NOT_EDITABLE"},
        )
    if message.flags & MESSAGE_FLAG_IS_VOICE_MESSAGE:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VOICE_MESSAGE_NOT_EDITABLE",
                "message": "Voice messages cannot be edited after they are sent.",
            },
        )
    if await session.get(Poll, (message.id, message.origin_domain)) is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "POLL_EDIT_UNSUPPORTED",
                "message": "Poll messages cannot be edited after they are sent.",
            },
        )


def validate_merged_message_edit(
    *,
    content: str | None,
    e2ee: dict[str, object] | None,
    embeds: Sequence[object],
    components: Sequence[object],
    attachment_count: int,
    sticker_items: Sequence[object],
    forward_snapshot: dict[str, object] | None,
    current_flags: int,
    requested_flags: int | None,
) -> bool:
    """Validate the complete stored result of a partial message edit."""

    encrypted_rich = isinstance(e2ee, dict) and "rich_payload_digest" in e2ee
    if (
        content is None
        and e2ee is None
        and not embeds
        and not components
        and attachment_count == 0
        and not sticker_items
        and forward_snapshot is None
    ):
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_BODY_REQUIRED"})
    if e2ee is not None and (
        embeds
        or components
        or (sticker_items and not encrypted_rich)
        or forward_snapshot is not None
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "MESSAGE_ENCRYPTION_POLICY_INVALID"},
        )
    effective_flags = requested_flags if requested_flags is not None else current_flags
    components_v2 = uses_components_v2(list(components)) or bool(
        encrypted_rich and effective_flags & MESSAGE_FLAG_IS_COMPONENTS_V2
    )
    if current_flags & MESSAGE_FLAG_IS_COMPONENTS_V2 and not components_v2:
        raise HTTPException(
            status_code=400,
            detail={"code": "COMPONENTS_V2_FLAG_IMMUTABLE"},
        )
    if (
        requested_flags is not None
        and requested_flags & MESSAGE_FLAG_IS_COMPONENTS_V2
        and not components_v2
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "COMPONENTS_V2_FLAG_REQUIRES_COMPONENTS_V2"},
        )
    if components_v2 and (content is not None or embeds or (sticker_items and not encrypted_rich)):
        raise HTTPException(
            status_code=400,
            detail={"code": "COMPONENTS_V2_CONTENT_INVALID"},
        )
    return components_v2


MAX_THREAD_MEMBERS = 1000
log = structlog.get_logger()


def require_voice_message_attachments(
    voice_message: bool,
    attachments: Sequence[Attachment | dict[str, object]],
) -> None:
    """Validate the immutable audio shape signalled by the voice-message flag."""

    def metadata(
        attachment: Attachment | dict[str, object],
    ) -> tuple[str | None, str | None, float | None, str | None]:
        if isinstance(attachment, Attachment):
            return (
                attachment.detected_content_type or attachment.content_type,
                attachment.encryption_mode,
                attachment.duration_secs,
                attachment.waveform,
            )
        raw_content_type = attachment.get("content_type")
        raw_encryption_mode = attachment.get("encryption_mode", "plaintext")
        raw_duration = attachment.get("duration_secs")
        raw_waveform = attachment.get("waveform")
        return (
            raw_content_type if isinstance(raw_content_type, str) else None,
            raw_encryption_mode if isinstance(raw_encryption_mode, str) else None,
            (
                float(raw_duration)
                if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool)
                else None
            ),
            raw_waveform if isinstance(raw_waveform, str) else None,
        )

    projections = [metadata(item) for item in attachments]
    if not voice_message:
        if any(
            duration is not None or waveform is not None for _, _, duration, waveform in projections
        ):
            raise HTTPException(
                status_code=400,
                detail={"code": "VOICE_MESSAGE_FLAG_REQUIRED"},
            )
        return
    if len(attachments) != 1:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VOICE_MESSAGE_ATTACHMENT_INVALID",
                "message": "A voice message requires exactly one audio attachment.",
            },
        )
    content_type, encryption_mode, duration_secs, waveform = projections[0]
    if encryption_mode == "e2ee":
        # Duration, waveform, and the original media type are authenticated
        # inside the encrypted rich body.  Requiring them in the outer
        # attachment projection would disclose private voice metadata and let
        # an untrusted relay influence playback UI.
        if duration_secs is not None or waveform is not None:
            raise HTTPException(
                status_code=400,
                detail={"code": "VOICE_MESSAGE_ATTACHMENT_INVALID"},
            )
        return
    if (
        content_type is None
        or not content_type.startswith("audio/")
        or encryption_mode != "plaintext"
        or duration_secs is None
        or duration_secs <= 0
        or waveform is None
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VOICE_MESSAGE_ATTACHMENT_INVALID",
                "message": (
                    "Voice messages require one plaintext audio attachment with duration and "
                    "waveform metadata."
                ),
            },
        )


@dataclass(frozen=True, slots=True)
class MessageAdmissionOptions:
    """Internal admission switches used by atomic thread creation.

    The dependency keeps these switches out of the public message-create body.
    Direct service reuse may pass an explicit instance; ordinary HTTP and bot
    callers receive the fail-closed defaults.
    """

    allow_required_e2ee_starter: bool = False
    mark_thread_starter: bool = False
    queue_thread_create: bool = False
    defer_dispatch: bool = False
    forum_starter_permissions_checked: bool = False
    forced_message_id: int | None = None
    replicated_attachments: tuple[dict[str, object], ...] = ()
    application_id: int | None = None
    application_domain: str | None = None
    bot_installation_id: int | None = None
    bot_user_installation_id: int | None = None
    bot_dm_capability_id: int | None = None
    bot_worker_id: int | None = None
    forward_source_e2ee_device_id: str | None = None
    webhook_id: int | None = None
    webhook_name: str | None = None
    webhook_avatar_hash: str | None = None
    webhook_avatar_url: str | None = None
    webhook_channel_id: int | None = None
    webhook_channel_domain: str | None = None
    webhook_e2ee_device_id: str | None = None
    tts: bool = False
    message_flags: int = 0
    required_attachment_binding_prefix: str | None = None
    required_attachment_purpose: str = "attachment"
    federated_guild_upload: bool = False
    skip_client_rate_limit: bool = False
    allow_render_only_components: bool = False
    automod_already_evaluated: bool = False
    automod_actor: User | None = None
    automod_permissions: int | None = None
    authoritative_mention_refs: tuple[tuple[int, str], ...] | None = None
    authoritative_mention_role_refs: tuple[tuple[int, str], ...] | None = None
    authoritative_mention_role_recipient_refs: tuple[tuple[int, str], ...] | None = None
    authoritative_mention_everyone: bool | None = None
    interaction_permissions: int | None = None
    interaction_message_type: int | None = None
    interaction_metadata: dict[str, object] | None = None
    poll_result: dict[str, object] | None = None
    transaction: MessageCreateTransaction | None = None

    def __post_init__(self) -> None:
        channel_bound = (
            self.webhook_channel_id is not None and self.webhook_channel_domain is not None
        )
        has_channel_field = (
            self.webhook_channel_id is not None or self.webhook_channel_domain is not None
        )
        if (self.webhook_id is not None and not channel_bound) or (
            self.webhook_id is None and has_channel_field
        ):
            raise ValueError(
                "webhook message admission requires both a webhook and its bound channel"
            )
        if self.webhook_e2ee_device_id is not None and self.webhook_id is None:
            raise ValueError("webhook E2EE device admission requires a webhook")
        if any(
            value is not None
            for value in (
                self.authoritative_mention_refs,
                self.authoritative_mention_role_refs,
                self.authoritative_mention_role_recipient_refs,
                self.authoritative_mention_everyone,
            )
        ) and (
            self.webhook_id is None
            and (self.application_id is None or self.application_domain is None)
        ):
            raise ValueError(
                "authoritative mention overrides require webhook or application admission"
            )
        if self.authoritative_mention_role_recipient_refs is not None and (
            self.authoritative_mention_refs is None
            or not set(self.authoritative_mention_role_recipient_refs).issubset(
                self.authoritative_mention_refs
            )
        ):
            raise ValueError(
                "authoritative role mention recipients require matching mention recipients"
            )
        bot_grants = (
            self.bot_installation_id,
            self.bot_user_installation_id,
            self.bot_dm_capability_id,
        )
        if any(value is not None for value in bot_grants) and (
            self.application_id is None or self.application_domain is None
        ):
            raise ValueError("bot installation admission requires an application identity")
        if sum(value is not None for value in bot_grants) > 1:
            raise ValueError("message admission accepts exactly one bot installation")
        if self.bot_worker_id is not None and (not any(value is not None for value in bot_grants)):
            raise ValueError("bot worker admission requires a bot installation")
        if self.forward_source_e2ee_device_id is not None and (
            self.application_id is None
            or self.application_domain is None
            or not any(value is not None for value in bot_grants)
        ):
            raise ValueError("forward source E2EE device requires a bot installation")
        if self.message_flags and self.webhook_id is None:
            raise ValueError("message flag overrides require webhook admission")
        if (self.automod_actor is None) != (self.automod_permissions is None):
            raise ValueError("AutoMod attribution requires an actor and permissions")
        if self.automod_actor is not None and (
            self.webhook_id is not None
            or self.bot_user_installation_id is None
            or self.application_id is None
            or self.application_domain is None
            or getattr(self.automod_actor, "account_type", "human") != "human"
            or isinstance(self.automod_permissions, bool)
            or not isinstance(self.automod_permissions, int)
            or self.automod_permissions < 0
            or self.automod_permissions & ~ALL_PERMISSIONS
        ):
            raise ValueError("AutoMod attribution is invalid")
        if self.interaction_permissions is not None:
            if (
                self.webhook_id is not None
                or self.application_id is None
                or self.application_domain is None
            ):
                raise ValueError(
                    "interaction permission snapshots require non-webhook application admission"
                )
            if (
                isinstance(self.interaction_permissions, bool)
                or self.interaction_permissions < 0
                or self.interaction_permissions & ~ALL_PERMISSIONS
            ):
                raise ValueError("interaction permission snapshot is invalid")
        if self.interaction_message_type is not None and (
            self.interaction_message_type not in {20, 23}
            or self.webhook_id is not None
            or self.application_id is None
            or self.application_domain is None
        ):
            raise ValueError("interaction message types require non-webhook application admission")
        if self.interaction_metadata is not None and (
            self.webhook_id is not None
            or self.application_id is None
            or self.application_domain is None
        ):
            raise ValueError("interaction metadata requires non-webhook application admission")
        if self.poll_result is not None:
            try:
                validate_poll_result_projection(self.poll_result)
            except ValueError as exc:
                raise ValueError("poll result admission is invalid") from exc
            if (
                self.webhook_id is not None
                or self.application_id is not None
                or self.application_domain is not None
                or any(value is not None for value in bot_grants)
                or self.interaction_message_type is not None
                or self.interaction_metadata is not None
            ):
                raise ValueError("poll result admission cannot claim application attribution")
        if self.webhook_id is None and (
            self.webhook_avatar_url is not None
            or self.required_attachment_purpose != "attachment"
            or self.federated_guild_upload
            or self.skip_client_rate_limit
        ):
            raise ValueError("webhook-only admission switches require a webhook")


async def message_view_installation_lineage(
    session: AsyncSession,
    settings: Settings,
    options: MessageAdmissionOptions | MessageMutationOptions,
    *,
    federated_transport: bool = False,
) -> tuple[str, int, str, int]:
    """Resolve the exact active grant that owns a newly interactive view.

    Database rows keep authority-local surrogate IDs for foreign keys.  A
    cross-authority proposal must instead carry the installation authority's
    stable reference so the receiving channel home can translate it to its
    own local surrogate before persisting a ``MessageView``.
    """

    installation: BotInstallation | BotUserInstallation | BotDMCapability | None
    if options.bot_installation_id is not None:
        installation = await session.get(BotInstallation, options.bot_installation_id)
        kind = "guild_install"
    elif options.bot_user_installation_id is not None:
        installation = await session.get(BotUserInstallation, options.bot_user_installation_id)
        kind = "user_install"
    elif options.bot_dm_capability_id is not None:
        installation = await session.get(BotDMCapability, options.bot_dm_capability_id)
        kind = "dm_capability"
    else:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_LINEAGE_REQUIRED"})
    if (
        installation is None
        or isinstance(installation, BotUserInstallation)
        and not user_installation_is_usable(
            installation,
            current_instance_domain=settings.domain,
        )
        or not isinstance(installation, BotUserInstallation)
        and getattr(installation, "status", None) != "active"
    ):
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_LINEAGE_INVALID"})
    revision = int(
        getattr(installation, "grant_revision", getattr(installation, "revision", 0)) or 0
    )
    if revision < 1:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_LINEAGE_INVALID"})
    if not federated_transport:
        return kind, int(installation.id), settings.domain, revision
    if isinstance(installation, BotInstallation):
        return kind, int(installation.id), installation.guild_domain, revision
    if isinstance(installation, BotUserInstallation):
        return (
            kind,
            int(installation.source_id or installation.id),
            installation.source_domain or installation.user_domain,
            revision,
        )
    return (
        kind,
        int(installation.source_installation_id),
        installation.source_installation_domain,
        revision,
    )


WEBHOOK_CAPABILITY_MESSAGE_PERMISSIONS = int(
    Permission.VIEW_CHANNEL
    | Permission.SEND_MESSAGES
    | Permission.SEND_MESSAGES_IN_THREADS
    | Permission.EMBED_LINKS
    | Permission.ATTACH_FILES
    | Permission.SEND_POLLS
    | Permission.SEND_TTS_MESSAGES
)


def require_attachment_upload_channel(attachment: Attachment, channel: Channel) -> None:
    """Keep a channel-scoped upload ticket bound to its authorized channel."""

    if attachment.upload_channel_id is not None and (
        attachment.upload_channel_id,
        attachment.upload_channel_domain,
    ) != (channel.id, channel.origin_domain):
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})


async def load_webhook_capability_channel_access(
    session: AsyncSession,
    settings: Settings,
    channel_ref: EntityReferenceLike,
    *,
    webhook_channel_id: int,
    webhook_channel_domain: str,
) -> ChannelAccess:
    """Load the channel fenced by an incoming-webhook capability.

    Incoming webhook tokens are the authority to publish; the creator's
    mutable role set is not. The target must still be the webhook's bound
    channel or one of its threads, and both the local channel and guild must
    remain available.
    """

    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    if channel_domain != settings.domain or webhook_channel_domain != settings.domain:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "WEBHOOK_DESTINATION_UNAVAILABLE",
                "message": "The webhook destination is no longer available.",
            },
        )
    channel = await session.scalar(
        select(Channel).where(
            Channel.id == channel_id,
            Channel.origin_domain == channel_domain,
            Channel.unavailable.is_(False),
        )
    )
    bound_directly = (channel_id, channel_domain) == (
        webhook_channel_id,
        webhook_channel_domain,
    )
    bound_thread = (
        channel is not None
        and channel.type in THREAD_CHANNEL_TYPES
        and (channel.parent_id, channel.parent_domain)
        == (webhook_channel_id, webhook_channel_domain)
    )
    if channel is None or not (bound_directly or bound_thread) or channel.guild_id is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "WEBHOOK_DESTINATION_UNAVAILABLE",
                "message": "The webhook destination is no longer available.",
            },
        )
    guild = await session.scalar(
        select(Guild).where(
            Guild.id == channel.guild_id,
            Guild.origin_domain == channel.guild_domain,
            Guild.unavailable.is_(False),
        )
    )
    if guild is None or guild.origin_domain != settings.domain:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "WEBHOOK_DESTINATION_UNAVAILABLE",
                "message": "The webhook guild is no longer available.",
            },
        )
    return ChannelAccess(channel=channel, guild=guild, participants=[])


async def load_interaction_permission_channel_access(
    session: AsyncSession,
    settings: Settings,
    channel_ref: EntityReferenceLike,
) -> ChannelAccess:
    """Load a local guild channel for an app response authorized at invocation.

    User-installed applications deliberately have no guild member row. Their
    response authorization is the invoking user's permission snapshot, so the
    ordinary actor-membership join cannot be used. DMs still load their real
    participant projection so dispatch remains correctly scoped.
    """

    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    if channel_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    channel = await session.scalar(
        select(Channel).where(
            Channel.id == channel_id,
            Channel.origin_domain == channel_domain,
            Channel.unavailable.is_(False),
        )
    )
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if channel.guild_id is not None:
        guild = await session.scalar(
            select(Guild).where(
                Guild.id == channel.guild_id,
                Guild.origin_domain == channel.guild_domain,
                Guild.origin_domain == settings.domain,
                Guild.unavailable.is_(False),
            )
        )
        if guild is None:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        return ChannelAccess(channel=channel, guild=guild, participants=[])
    if channel.type != 1:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
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
            .order_by(User.origin_domain, User.username)
        )
    )
    if not participants:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    return ChannelAccess(channel=channel, guild=None, participants=participants)


@dataclass(slots=True)
class MessageCreatePostCommit:
    """Message projections that are safe only after the owning SQL commit."""

    automod: AutoModPostCommit | None
    access: ChannelAccess
    message: Message
    result: dict[str, object]
    attachments: list[Attachment]
    remote_destinations: set[str]
    mark_thread_starter: bool
    defer_dispatch: bool
    thread_was_unarchived: bool
    added_thread_members: list[ThreadMember]
    dm_history_changed: bool
    dm_conversation: DMConversation | None
    rendered_thread: dict[str, object] | None

    async def publish(
        self,
        session: AsyncSession,
        redis: Redis,
        settings: Settings,
    ) -> None:
        """Run the existing best-effort message projections after commit."""

        if self.automod is not None:
            await self.automod.publish(redis)
        channel = self.access.channel
        try:
            await cast(
                Awaitable[object],
                redis.eval(
                    SET_LATEST_MESSAGE_SCRIPT,
                    1,
                    f"channel:last_message:{channel.origin_domain}:{channel.id}",
                    str(self.message.id),
                    self.message.origin_domain,
                ),
            )
            publish_thread_activity = (
                channel.type in THREAD_CHANNEL_TYPES and not self.mark_thread_starter
            )
            if publish_thread_activity:
                if self.rendered_thread is None:
                    raise RuntimeError("thread projection was not materialized before commit")
                await publish_channel_dispatch(
                    redis,
                    self.access,
                    "THREAD_UPDATE",
                    self.rendered_thread,
                )
            if self.thread_was_unarchived and self.access.guild is not None:
                await publish_current_thread_member_updates(
                    session,
                    redis,
                    self.access.guild,
                    channel,
                )
            if self.added_thread_members and self.access.guild is not None:
                rendered_members = [
                    thread_member_payload(member) for member in self.added_thread_members
                ]
                rich_rendered_members = [
                    await rich_thread_member_payload(session, member)
                    for member in self.added_thread_members
                ]
                topic = guild_topic(self.access.guild.origin_domain, self.access.guild.id)
                for rendered_member in rendered_members:
                    target_ref = f"{rendered_member['user_id']}@{rendered_member['user_domain']}"
                    if self.rendered_thread is None:
                        raise RuntimeError("thread projection was not materialized before commit")
                    await publish_dispatch(
                        redis,
                        topic,
                        "THREAD_CREATE",
                        self.rendered_thread | {"member": rendered_member},
                        audience_user_refs=[target_ref],
                    )
                    await publish_dispatch(
                        redis,
                        topic,
                        "THREAD_MEMBER_UPDATE",
                        rendered_member,
                        audience_user_refs=[target_ref],
                    )
                await publish_dispatch(
                    redis,
                    topic,
                    "THREAD_MEMBERS_UPDATE",
                    {
                        "id": str(channel.id),
                        "thread_domain": channel.origin_domain,
                        "guild_id": str(self.access.guild.id),
                        "guild_domain": self.access.guild.origin_domain,
                        "member_count": min(50, int(channel.member_count or 0)),
                        "added_members": rich_rendered_members,
                        "removed_member_ids": [],
                    },
                )
            if not self.defer_dispatch:
                await publish_channel_dispatch(redis, self.access, "MESSAGE_CREATE", self.result)
            if (
                self.access.guild is None
                and self.dm_history_changed
                and self.dm_conversation is not None
            ):
                history = dm_history_metadata(
                    self.dm_conversation,
                    local_domain=settings.domain,
                    remote_available=await dm_authority_history_available(
                        session,
                        self.dm_conversation,
                        local_domain=settings.domain,
                    ),
                )
                for participant in self.access.participants:
                    if participant.origin_domain != settings.domain or not participant.is_local:
                        continue
                    await publish_dispatch(
                        redis,
                        user_topic(settings.domain, participant.id),
                        "CHANNEL_UPDATE",
                        dm_channel_payload(
                            channel,
                            [
                                user
                                for user in self.access.participants
                                if (user.id, user.origin_domain)
                                != (participant.id, participant.origin_domain)
                            ],
                            conversation=self.dm_conversation,
                            history=history,
                        ),
                    )
        except Exception:
            log.exception(
                "message_postcommit_projection_failed",
                message_id=str(self.message.id),
                message_domain=self.message.origin_domain,
            )
        # These workflows have durable SQL sources and must still be woken when
        # an unrelated Redis cache/fanout projection above fails.
        await enqueue_best_effort(mentions_fanout, self.message.id, self.message.origin_domain)
        for destination in self.remote_destinations:
            await enqueue_best_effort(federation_deliver, destination)
        for attachment in self.attachments:
            await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)


@dataclass(slots=True)
class MessageCreateTransaction:
    """Own a no-commit message mutation and its deferred side effects."""

    postcommits: list[MessageCreatePostCommit] = field(default_factory=list)

    def stage(self, postcommit: MessageCreatePostCommit) -> None:
        self.postcommits.append(postcommit)

    async def commit(
        self,
        session: AsyncSession,
        redis: Redis,
        settings: Settings,
    ) -> None:
        await session.commit()
        postcommits, self.postcommits = self.postcommits, []
        for postcommit in postcommits:
            await postcommit.publish(session, redis, settings)


@dataclass(frozen=True, slots=True)
class MessageMutationOptions:
    """Authenticated application identity for component-view mutations."""

    application_id: int | None = None
    application_domain: str | None = None
    bot_installation_id: int | None = None
    bot_user_installation_id: int | None = None
    bot_dm_capability_id: int | None = None
    bot_worker_id: int | None = None
    trusted_external_domain: str | None = None
    authoritative_mention_refs: tuple[tuple[int, str], ...] | None = None
    authoritative_mention_role_refs: tuple[tuple[int, str], ...] | None = None
    authoritative_mention_everyone: bool | None = None
    authoritative_attachment_refs: tuple[tuple[int, str], ...] | None = None
    replicated_attachments: tuple[dict[str, object], ...] = ()
    automod_actor: User | None = None
    automod_permissions: int | None = None
    webhook_id: int | None = None
    webhook_channel_id: int | None = None
    webhook_channel_domain: str | None = None
    webhook_e2ee_device_id: str | None = None
    required_attachment_binding_prefix: str | None = None
    required_attachment_purpose: str = "attachment"
    allow_render_only_components: bool = False
    expression_authorization_checked: bool = False
    attested_expression_tokens: tuple[str, ...] = ()
    attested_expression_sticker_items: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        webhook_channel_bound = (
            self.webhook_channel_id is not None and self.webhook_channel_domain is not None
        )
        if (self.webhook_id is not None) != webhook_channel_bound:
            raise ValueError("webhook edit admission requires its exact bound channel")
        if self.webhook_e2ee_device_id is not None and self.webhook_id is None:
            raise ValueError("webhook E2EE edit admission requires a webhook")
        if self.webhook_id is None and (
            self.required_attachment_binding_prefix is not None
            or self.required_attachment_purpose != "attachment"
            or self.allow_render_only_components
        ):
            raise ValueError("webhook edit switches require webhook admission")
        if any(
            value is not None
            for value in (
                self.authoritative_mention_refs,
                self.authoritative_mention_role_refs,
                self.authoritative_mention_everyone,
            )
        ) and (self.application_id is None or self.application_domain is None):
            raise ValueError("authoritative mention edits require application admission")
        if self.replicated_attachments and self.authoritative_attachment_refs is None:
            raise ValueError("replicated edit attachments require authoritative references")
        if not self.expression_authorization_checked and (
            self.attested_expression_tokens or self.attested_expression_sticker_items
        ):
            raise ValueError("attested expressions require authorization validation")
        if (self.automod_actor is None) != (self.automod_permissions is None):
            raise ValueError("AutoMod attribution requires an actor and permissions")
        if self.automod_actor is not None and (
            self.bot_user_installation_id is None
            or self.application_id is None
            or self.application_domain is None
            or getattr(self.automod_actor, "account_type", "human") != "human"
            or isinstance(self.automod_permissions, bool)
            or not isinstance(self.automod_permissions, int)
            or self.automod_permissions < 0
            or self.automod_permissions & ~ALL_PERMISSIONS
        ):
            raise ValueError("AutoMod attribution is invalid")


def default_message_mutation_options() -> MessageMutationOptions:
    return MessageMutationOptions()


def require_available_edit_attachment(
    attachment: Attachment,
    channel: Channel,
    options: MessageMutationOptions,
) -> None:
    """Apply the same channel and ownership fence to every attachment edit."""

    require_attachment_upload_channel(attachment, channel)
    expected_guild_installation = options.bot_installation_id
    expected_user_installation = options.bot_user_installation_id
    expected_dm_capability = options.bot_dm_capability_id
    owner_matches = (
        attachment.bot_installation_id == expected_guild_installation
        and attachment.bot_user_installation_id == expected_user_installation
        and attachment.bot_dm_capability_id == expected_dm_capability
        if any(
            value is not None
            for value in (
                expected_guild_installation,
                expected_user_installation,
                expected_dm_capability,
            )
        )
        else attachment.bot_installation_id is None
        and attachment.bot_user_installation_id is None
        and attachment.bot_dm_capability_id is None
    )
    expected_binding = (
        f"{options.required_attachment_binding_prefix}{attachment.id}"
        if options.required_attachment_binding_prefix is not None
        else None
    )
    if (
        not owner_matches
        or attachment.message_id is not None
        or attachment.message_domain is not None
        or attachment.interaction_id is not None
        or attachment.interaction_response_id is not None
        or attachment.asset_binding != expected_binding
        or attachment.report_id is not None
        or attachment.encryption_mode
        != ("e2ee" if channel.encryption_mode == "e2ee" else "plaintext")
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "ATTACHMENT_NOT_FOUND"},
        )


async def mark_guild_activity(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
) -> None:
    """Advance the authoritative member activity clock in the same transaction."""

    if access.guild is None or access.guild.origin_domain != settings.domain:
        return
    await session.execute(
        update(GuildMember)
        .where(
            GuildMember.guild_id == access.guild.id,
            GuildMember.guild_domain == access.guild.origin_domain,
            GuildMember.user_id == actor.id,
            GuildMember.user_domain == actor.origin_domain,
        )
        .values(last_guild_activity_at=datetime.now(UTC))
    )


def default_message_admission_options() -> MessageAdmissionOptions:
    return MessageAdmissionOptions()


async def load_poll_result_channel_access(
    session: AsyncSession,
    settings: Settings,
    channel_ref: EntityReferenceLike,
) -> ChannelAccess:
    """Load the local authority channel for a system-generated poll result."""

    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel = await session.get(Channel, (channel_id, channel_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.origin_domain != settings.domain
        or not is_message_capable_channel_type(
            channel.type,
            guild_channel=channel.guild_id is not None,
        )
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if channel.guild_id is not None:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is None or guild.unavailable or guild.origin_domain != settings.domain:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        return ChannelAccess(channel=channel, guild=guild, participants=[])
    if channel.type != 1:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
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
            .order_by(User.origin_domain, User.username)
        )
    )
    return ChannelAccess(channel=channel, guild=None, participants=participants)


async def validate_poll_result_admission(
    session: AsyncSession,
    channel: Channel,
    author: User,
    payload: MessageCreate,
    projection: dict[str, object],
) -> Message:
    """Recompute the exact result body; callers cannot forge system type 46."""

    source_ref_text = projection.get("poll_message_ref")
    if not isinstance(source_ref_text, str):
        raise RuntimeError("validated poll result lost its source reference")
    source_ref = EntityRef(source_ref_text).resolve(channel.origin_domain)
    source = await session.scalar(
        select(Message)
        .where(
            Message.id == source_ref[0],
            Message.origin_domain == source_ref[1],
            Message.channel_id == channel.id,
            Message.channel_domain == channel.origin_domain,
            Message.deleted_at.is_(None),
        )
        .with_for_update()
    )
    poll = await session.scalar(
        select(Poll)
        .where(
            Poll.message_id == source_ref[0],
            Poll.message_domain == source_ref[1],
            Poll.finalized_at.is_not(None),
        )
        .with_for_update()
    )
    if (
        source is None
        or poll is None
        or (source.author_id, source.author_domain) != (author.id, author.origin_domain)
    ):
        raise HTTPException(status_code=409, detail={"code": "POLL_RESULT_SOURCE_INVALID"})
    expected_projection, expected_embed = await poll_result_material(
        session,
        source,
        poll,
    )
    rendered_embeds = [item.model_dump(mode="json", exclude_none=True) for item in payload.embeds]
    expected_request_embed = {
        str(key): value for key, value in expected_embed.items() if key != "type"
    }
    if (
        projection != expected_projection
        or payload.content is not None
        or payload.e2ee is not None
        or payload.tts
        or payload.voice_message
        or payload.flags
        or rendered_embeds != [expected_request_embed]
        or payload.components
        or payload.poll is not None
        or payload.sticker_ids
        or payload.forwarded_message_id is not None
        or payload.forward_source_proof is not None
        or payload.forward_snapshot is not None
        or payload.referenced_message_id is None
        or payload.referenced_message_id.resolve(channel.origin_domain)
        != (source.id, source.origin_domain)
        or [item.resolve(channel.origin_domain) for item in payload.mention_user_ids]
        != [(author.id, author.origin_domain)]
        or payload.attachment_ids
    ):
        raise HTTPException(status_code=409, detail={"code": "POLL_RESULT_BODY_INVALID"})
    return source


async def poll_result_material(
    session: AsyncSession,
    source: Message,
    poll: Poll,
) -> tuple[dict[str, object], dict[str, object]]:
    """Derive the one canonical label-free projection and visual result embed."""

    answers = list(
        await session.scalars(
            select(PollAnswer)
            .where(
                PollAnswer.message_id == source.id,
                PollAnswer.message_domain == source.origin_domain,
            )
            .order_by(PollAnswer.answer_id)
        )
    )
    counts = {
        int(answer_id): int(count)
        for answer_id, count in (
            await session.execute(
                select(PollVote.answer_id, func.count())
                .where(
                    PollVote.message_id == source.id,
                    PollVote.message_domain == source.origin_domain,
                )
                .group_by(PollVote.answer_id)
            )
        ).all()
    }
    expected_projection = build_poll_result_projection(
        source_ref=(source.id, source.origin_domain),
        answer_counts=[(answer.answer_id, counts.get(answer.answer_id, 0)) for answer in answers],
        source_encryption_mode=("e2ee" if source.e2ee is not None else "plaintext"),
    )
    expected_embed = poll_result_embed(
        expected_projection,
        question=poll.question,
        answers=[(answer.answer_id, answer.text, answer.emoji) for answer in answers],
    )
    return expected_projection, expected_embed


def require_unarchived_thread(channel: Channel) -> None:
    if channel.type in THREAD_CHANNEL_TYPES and bool(channel.archived):
        raise HTTPException(status_code=409, detail={"code": "THREAD_ARCHIVED"})


def require_thread_message_delete_state(channel: Channel, permissions: int) -> None:
    """Enforce Discord's locked-and-archived deletion boundary.

    Message deletion is the one ordinary mutation allowed in an archived
    thread. A locked thread narrows that exception: non-moderators may delete
    only while the thread is active. MANAGE_THREADS holders retain moderation
    access in either lifecycle state.
    """

    if (
        channel.type in THREAD_CHANNEL_TYPES
        and bool(channel.archived)
        and bool(channel.locked)
        and not permissions & Permission.MANAGE_THREADS
    ):
        raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})


def require_message_encryption_policy(
    channel: Channel,
    *,
    content: object,
    e2ee: object,
    attachment_count: int = 0,
    allow_required_e2ee_starter: bool = False,
) -> None:
    if (
        channel.type in THREAD_CHANNEL_TYPES
        and bool(getattr(channel, "e2ee_required", False))
        and channel.encryption_mode != "e2ee"
        and not allow_required_e2ee_starter
    ):
        raise HTTPException(status_code=409, detail={"code": "THREAD_E2EE_ACTIVATION_REQUIRED"})
    if channel.encryption_mode == "e2ee" and channel.encryption_state != "active":
        raise HTTPException(status_code=409, detail={"code": "E2EE_REKEY_REQUIRED"})
    try:
        validate_message_encryption_policy(
            channel.encryption_mode,
            content=content,
            e2ee=e2ee,
            attachment_count=attachment_count,
            policy_generation=channel.encryption_policy_generation,
            policy_epoch=channel.encryption_epoch,
            policy_group_id=channel.encryption_group_id,
        )
    except MessageEncryptionPolicyError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc


async def require_forward_age_context(
    session: AsyncSession,
    source_channel: Channel,
    destination_channel: Channel,
) -> bool:
    """Validate both authoritative channel contexts and return the source state."""

    source_nsfw = await effective_channel_nsfw(session, source_channel)
    destination_nsfw = await effective_channel_nsfw(session, destination_channel)
    if not can_forward_between_age_contexts(source_nsfw, destination_nsfw):
        raise HTTPException(
            status_code=409,
            detail={"code": "AGE_RESTRICTED_FORWARD_UNSUPPORTED"},
        )
    if source_nsfw is None:
        raise RuntimeError("validated forward source age context disappeared")
    return source_nsfw


async def bot_can_join_e2ee_thread(
    session: AsyncSession,
    guild: Guild,
    thread: Channel,
    bot: User,
) -> bool:
    """Require an active participant installation and a staged/trusted MLS device."""

    installation = await session.scalar(
        select(BotInstallation)
        .join(
            BotApplication,
            (BotApplication.id == BotInstallation.application_id)
            & (BotApplication.origin_domain == BotInstallation.application_domain),
        )
        .where(
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.bot_user_id == bot.id,
            BotInstallation.bot_user_domain == bot.origin_domain,
            usable_guild_installation(),
            BotInstallation.e2ee_mode == "participant",
            BotApplication.status == "active",
        )
        .limit(1)
    )
    if installation is None:
        return False
    return (
        await active_bot_e2ee_participation(
            session,
            installation,
            thread,
            None,
            include_pending=True,
        )
        is not None
    )


async def admit_thread_message_members(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    thread: Channel,
    actor: User,
    actor_permissions: int,
    mention_pairs: list[tuple[int, str]],
    role_mention_pairs: set[tuple[int, str]],
    *,
    admit_actor: bool = True,
) -> tuple[list[ThreadMember], bool, bool]:
    """Auto-join a sender and eligible mentions without a second message transaction."""

    actor_ref = (actor.id, actor.origin_domain)
    ordered_refs = list(dict.fromkeys(([actor_ref] if admit_actor else []) + list(mention_pairs)))
    if not ordered_refs:
        return [], False, False
    existing_members = {
        (member.user_id, member.user_domain): member
        for member in await session.scalars(
            select(ThreadMember).where(
                ThreadMember.thread_id == thread.id,
                ThreadMember.thread_domain == thread.origin_domain,
                tuple_(ThreadMember.user_id, ThreadMember.user_domain).in_(ordered_refs),
            )
        )
    }
    missing_refs = [ref for ref in ordered_refs if ref not in existing_members]
    if not missing_refs:
        return [], False, False

    users = {
        (user.id, user.origin_domain): user
        for user in await session.scalars(
            select(User).where(tuple_(User.id, User.origin_domain).in_(missing_refs))
        )
    }
    parent = await session.get(Channel, (thread.parent_id, thread.parent_domain))
    if parent is None or parent.type not in {0, 5, 15}:
        raise HTTPException(status_code=409, detail={"code": "THREAD_PARENT_INVALID"})

    may_invite_private = bool(
        thread.type != 12 or thread.invitable or actor_permissions & Permission.MANAGE_THREADS
    )
    added: list[ThreadMember] = []
    failed_role_mentions = False
    for user_ref in missing_refs:
        user = users.get(user_ref)
        is_actor = user_ref == actor_ref
        eligible = user is not None
        if (
            eligible
            and user is not None
            and user.account_type == "bot"
            and (thread.e2ee_required or thread.encryption_mode == "e2ee")
        ):
            eligible = await bot_can_join_e2ee_thread(session, guild, thread, user)
        if not is_actor:
            eligible = eligible and may_invite_private
            if eligible and user is not None:
                target_permissions = await get_permissions(
                    session, redis, guild, user, channel=parent
                )
                eligible = bool(target_permissions & Permission.VIEW_CHANNEL)
        if not eligible:
            failed_role_mentions |= user_ref in role_mention_pairs
            continue
        if int(thread.member_count or 0) + len(added) >= MAX_THREAD_MEMBERS:
            if is_actor:
                raise HTTPException(status_code=409, detail={"code": "THREAD_MEMBER_LIMIT"})
            failed_role_mentions |= user_ref in role_mention_pairs
            continue
        member = ThreadMember(
            thread_id=thread.id,
            thread_domain=thread.origin_domain,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=user_ref[0],
            user_domain=user_ref[1],
            flags=0,
            notification_level="inherit",
        )
        session.add(member)
        added.append(member)

    if not added:
        return [], False, failed_role_mentions
    thread.member_count = int(thread.member_count or 0) + len(added)
    private_access_changed = thread.type == 12 and any(
        (member.user_id, member.user_domain) != (actor.id, actor.origin_domain) for member in added
    )
    thread_rekeyed = False
    if private_access_changed:
        guild.permission_generation += 1
        if thread.encryption_mode == "e2ee" and thread.encryption_state == "active":
            thread.encryption_state = "rekeying"
            thread_rekeyed = True
    await session.flush()
    for index, member in enumerate(added):
        await queue_guild_mutation(
            session,
            settings,
            guild,
            actor,
            "guild.thread.member.upsert",
            {
                "member": thread_member_payload(member),
                "member_count": thread.member_count,
            },
            channel=thread,
            snapshot_required=private_access_changed and index == len(added) - 1,
        )
    return added, thread_rekeyed, failed_role_mentions


def capture_thread_message_projection(channel: Channel) -> dict[str, object]:
    """Capture fields advanced exclusively by the next message delta."""

    return {
        "message_count": channel.message_count,
        "total_message_sent": channel.total_message_sent,
        "last_activity_at": (
            channel.last_activity_at.isoformat() if channel.last_activity_at is not None else None
        ),
    }


def thread_structural_state_before_message(
    channel: Channel,
    prior_message_projection: dict[str, object],
) -> dict[str, object]:
    """Render lifecycle/MLS changes without pre-applying the next message."""

    state = federation_channel_state(channel)
    state.update(prior_message_projection)
    return state


async def publish_current_thread_member_updates(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    thread: Channel,
) -> None:
    """Hydrate every joined client after an unarchive transition.

    Discord follows THREAD_UPDATE with each recipient's current membership.
    Audience-targeting keeps private membership metadata from becoming a
    guild-wide event while retaining deterministic event ordering.
    """

    members = list(
        await session.scalars(
            select(ThreadMember)
            .where(
                ThreadMember.thread_id == thread.id,
                ThreadMember.thread_domain == thread.origin_domain,
            )
            .order_by(ThreadMember.user_domain, ThreadMember.user_id)
        )
    )
    topic = guild_topic(guild.origin_domain, guild.id)
    for member in members:
        await publish_dispatch(
            redis,
            topic,
            "THREAD_MEMBER_UPDATE",
            thread_member_payload(member),
            audience_user_refs=[f"{member.user_id}@{member.user_domain}"],
        )


async def refresh_thread_last_message_after_delete(
    session: AsyncSession,
    thread: Channel,
) -> None:
    latest = await session.scalar(
        select(Message)
        .where(
            Message.channel_id == thread.id,
            Message.channel_domain == thread.origin_domain,
            Message.deleted_at.is_(None),
        )
        .order_by(Message.created_at.desc(), Message.id.desc(), Message.origin_domain.desc())
        .limit(1)
    )
    if latest is None:
        # A source-backed starter lives in the parent and cannot satisfy the
        # FK-backed child cursor.
        thread.last_message_id = None
        thread.last_message_domain = None
        return
    thread.last_message_id = latest.id
    thread.last_message_domain = latest.origin_domain


async def publish_replica_guild_status(redis: Redis, guild: Guild) -> None:
    """Best-effort live projection for a replica quota pause or recovery."""

    try:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_UPDATE",
            guild_payload(guild),
        )
    except Exception:
        log.exception(
            "replica_guild_status_publish_failed",
            guild_id=str(guild.id),
            guild_domain=guild.origin_domain,
        )


def raise_proxy_rejection(response: httpx.Response, statuses: set[int]) -> None:
    """Preserve bounded, typed peer errors for synchronous proxy operations."""

    if response.status_code not in statuses:
        return
    try:
        error_body = decode_federation_response_json(response)
    except FederationNetworkError:
        error_body = None
    raise HTTPException(
        status_code=response.status_code,
        detail=parse_upstream_error(error_body, "FEDERATED_WRITE_REJECTED"),
    )


def validate_federated_boolean_ack(
    payload: object,
    field: str,
    expected: bool,
) -> dict[str, object]:
    """Accept only the one-field acknowledgement emitted by an authority."""

    if (
        not isinstance(payload, dict)
        or set(payload) != {field}
        or payload.get(field) is not expected
    ):
        raise ValueError("federated acknowledgement is invalid")
    return cast(dict[str, object], payload)


FEDERATED_USER_PAGE_PROFILE_KEYS = frozenset(
    {
        "id",
        "origin_domain",
        "username",
        "display_name",
        "avatar_hash",
        "banner_hash",
        "bio",
        "custom_status",
        "profile_version",
        "e2ee_device_generation",
        "profile_resolved",
        "handle",
        "account_type",
        "bot",
    }
)


def validate_federated_user_page(
    payload: object,
    *,
    collection: Literal["items", "users"],
    after: tuple[int, str] | None,
    limit: int,
    include_total: bool,
) -> dict[str, object]:
    """Validate a cursor page without trusting peer ordering or identities."""

    expected_keys = {collection, "next_after"} | ({"total"} if include_total else set())
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ValueError("federated user page has an invalid shape")
    raw_users = payload.get(collection)
    if not isinstance(raw_users, list) or len(raw_users) > limit:
        raise ValueError("federated user page exceeds its requested limit")
    if include_total:
        total = payload.get("total")
        if type(total) is not int or total < len(raw_users):
            raise ValueError("federated user page total is invalid")

    refs: list[tuple[int, str]] = []
    for raw_user in raw_users:
        if not isinstance(raw_user, dict) or set(raw_user) != FEDERATED_USER_PAGE_PROFILE_KEYS:
            raise ValueError("federated user page profile has an invalid shape")
        user_id = validate_snowflake(raw_user.get("id"))
        domain = normalize_domain(str(raw_user.get("origin_domain", "")))
        if raw_user.get("origin_domain") != domain:
            raise ValueError("federated user page profile domain is not canonical")
        account_type = raw_user.get("account_type")
        username = raw_user.get("username")
        if (
            account_type not in {"human", "bot"}
            or raw_user.get("bot") is not (account_type == "bot")
            or not isinstance(username, str)
            or raw_user.get("handle") != f"{username}@{domain}"
            or type(raw_user.get("profile_resolved")) is not bool
        ):
            raise ValueError("federated user page profile is invalid")
        for version_field in ("profile_version", "e2ee_device_generation"):
            raw_version = raw_user.get(version_field)
            if (
                not isinstance(raw_version, str)
                or str(validate_snowflake(raw_version)) != raw_version
            ):
                raise ValueError("federated user page profile version is invalid")
        ref = (user_id, domain)
        if (after is not None and ref <= after) or (refs and ref <= refs[-1]):
            raise ValueError("federated user page ordering is invalid")
        refs.append(ref)

    next_after = payload.get("next_after")
    if next_after is not None:
        if not isinstance(next_after, str) or not refs:
            raise ValueError("federated user page cursor is invalid")
        parsed_cursor = EntityRef(next_after).resolve("")
        if "@" not in next_after or parsed_cursor != refs[-1]:
            raise ValueError("federated user page cursor does not match its page")
    return cast(dict[str, object], payload)


def validate_federated_message_operation_response(
    payload: object,
    *,
    operation: str,
    channel_ref: tuple[int, str],
    message_ref: tuple[int, str] | None,
    after: tuple[int, str] | None = None,
    limit: int = 50,
) -> dict[str, object]:
    """Validate one operation-specific authority response before exposing it."""

    acknowledgements = {
        "message.delete": ("deleted", True),
        "message.bulk_delete": ("deleted", True),
        "reaction.remove_user": ("removed", True),
        "reaction.clear": ("removed", True),
        "reaction.add": ("updated", True),
        "reaction.remove": ("updated", True),
        "poll.vote.add": ("updated", True),
        "poll.vote.remove": ("updated", True),
        "pin.add": ("updated", True),
        "pin.remove": ("updated", True),
    }
    acknowledgement = acknowledgements.get(operation)
    if acknowledgement is not None:
        return validate_federated_boolean_ack(payload, *acknowledgement)
    if operation == "reaction.list":
        return validate_federated_user_page(
            payload,
            collection="items",
            after=after,
            limit=limit,
            include_total=True,
        )
    if operation == "poll.voters.list":
        return validate_federated_user_page(
            payload,
            collection="users",
            after=after,
            limit=limit,
            include_total=False,
        )
    if operation not in {"message.edit", "announcement.crosspost", "poll.end"}:
        raise ValueError("federated message operation is unsupported")
    if not isinstance(payload, dict) or set(payload) != {"message"}:
        raise ValueError("federated message response has an invalid shape")
    message = payload.get("message")
    if not isinstance(message, dict) or message_ref is None:
        raise ValueError("federated message response omitted its message")
    if (
        message.get("id") != str(message_ref[0])
        or message.get("origin_domain") != message_ref[1]
        or message.get("channel_id") != str(channel_ref[0])
        or message.get("channel_domain") != channel_ref[1]
    ):
        raise ValueError("federated message response escaped its requested resource")
    if operation == "announcement.crosspost" and message.get("published_at") is None:
        raise ValueError("federated crosspost response is not published")
    if operation == "poll.end":
        poll = message.get("poll")
        results = poll.get("results") if isinstance(poll, dict) else None
        if not isinstance(results, dict) or results.get("is_finalized") is not True:
            raise ValueError("federated poll response is not finalized")
    return cast(dict[str, object], payload)


async def require_channel_permissions(
    session: AsyncSession,
    redis: Redis,
    access: ChannelAccess,
    actor: User,
    permissions: Permission,
) -> int:
    if access.guild is not None:
        permissions = channel_message_permissions(access.channel, permissions)
        return await require_permissions(
            session,
            redis,
            access.guild,
            actor,
            permissions,
            channel=access.channel,
        )
    return int(Permission.EMBED_LINKS | Permission.ATTACH_FILES | Permission.SEND_MESSAGES)


def channel_message_permissions(channel: Channel, permissions: Permission) -> Permission:
    """Map the guild text-send permission to its thread-specific equivalent."""

    if channel.type in THREAD_CHANNEL_TYPES and permissions & Permission.SEND_MESSAGES:
        return Permission(
            (int(permissions) & ~int(Permission.SEND_MESSAGES))
            | int(Permission.SEND_MESSAGES_IN_THREADS)
        )
    return permissions


def message_create_permissions(
    payload: MessageCreate,
    *,
    guild_channel: bool,
    forum_starter_permissions_checked: bool = False,
) -> Permission:
    required = (
        Permission.VIEW_CHANNEL
        if forum_starter_permissions_checked
        else required_permissions("message.create")
    )
    if payload.attachment_ids:
        required |= Permission.ATTACH_FILES
    if payload.voice_message and guild_channel:
        required |= Permission.SEND_VOICE_MESSAGES
    if payload.tts and guild_channel:
        required |= Permission.SEND_TTS_MESSAGES
    _contract, _controls, encrypted_poll = encrypted_rich_routing(payload.e2ee)
    if payload.poll is not None or encrypted_poll is not None:
        required |= required_permissions("poll.create")
    return required


async def require_valid_message_mentions(
    session: AsyncSession,
    access: ChannelAccess,
    mention_pairs: Sequence[tuple[int, str]],
) -> None:
    """Verify that every pre-resolved notification recipient belongs here."""

    if access.guild is None:
        allowed_mentions = {
            (participant.id, participant.origin_domain) for participant in access.participants
        }
    elif mention_pairs:
        allowed_mentions = set(
            (
                await session.execute(
                    select(GuildMember.user_id, GuildMember.user_domain).where(
                        GuildMember.guild_id == access.guild.id,
                        GuildMember.guild_domain == access.guild.origin_domain,
                        tuple_(GuildMember.user_id, GuildMember.user_domain).in_(mention_pairs),
                    )
                )
            ).tuples()
        )
    else:
        allowed_mentions = set()
    if any(item not in allowed_mentions for item in mention_pairs):
        raise HTTPException(status_code=400, detail={"code": "INVALID_MENTION"})


def _encrypted_mention_refs(envelope: dict[str, object], field: str) -> list[tuple[int, str]]:
    raw = envelope.get(field)
    if not isinstance(raw, list):
        raise HTTPException(status_code=409, detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"})
    try:
        return [EntityRef(item).resolve("") for item in raw if isinstance(item, str)]
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
        ) from exc


async def resolve_encrypted_rich_mention_projection(
    session: AsyncSession,
    access: ChannelAccess,
    envelope: object,
    *,
    actor_permissions: int,
    referenced: Message | None,
) -> ResolvedMentions | None:
    """Expand authenticated E2EE mention intent without seeing private text."""

    if not isinstance(envelope, dict) or "rich_payload_digest" not in envelope:
        return None
    users = _encrypted_mention_refs(envelope, "message_mention_user_refs")
    roles = _encrypted_mention_refs(envelope, "message_mention_role_refs")
    recipients = set(users)
    role_recipients: set[tuple[int, str]] = set()
    mention_everyone = envelope.get("message_mention_everyone")
    if not isinstance(mention_everyone, bool):
        raise HTTPException(status_code=409, detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"})
    if roles or mention_everyone:
        if access.guild is None:
            raise HTTPException(status_code=400, detail={"code": "MENTION_CONTEXT_INVALID"})
        if roles:
            role_text = " ".join(f"<@&{role_id}@{role_domain}>" for role_id, role_domain in roles)
            role_recipients.update(
                await role_mention_recipients(session, access.guild, role_text, actor_permissions)
            )
            recipients.update(role_recipients)
        if mention_everyone:
            recipients.update(
                await everyone_mention_recipients(
                    session,
                    access,
                    Permission(actor_permissions),
                )
            )
    replied_user_ref = envelope.get("message_replied_user_ref")
    if replied_user_ref is not None:
        if not isinstance(replied_user_ref, str) or referenced is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
            )
        expected_reply_ref = f"{referenced.author_id}@{referenced.author_domain}"
        if replied_user_ref != expected_reply_ref:
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
            )
        recipients.add((referenced.author_id, referenced.author_domain))
    resolved = sorted(recipients)
    expected = [f"{user_id}@{domain}" for user_id, domain in resolved]
    if envelope.get("message_mention_refs") != expected:
        raise HTTPException(status_code=409, detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"})
    await require_valid_message_mentions(session, access, resolved)
    return ResolvedMentions(
        recipients=tuple(resolved),
        roles=tuple(roles),
        everyone=mention_everyone,
        role_recipients=tuple(sorted(role_recipients)),
    )


async def resolve_encrypted_rich_mentions(
    session: AsyncSession,
    access: ChannelAccess,
    envelope: object,
    *,
    actor_permissions: int,
    referenced: Message | None,
) -> list[tuple[int, str]] | None:
    """Compatibility projection for callers that need recipients only."""

    resolved = await resolve_encrypted_rich_mention_projection(
        session,
        access,
        envelope,
        actor_permissions=actor_permissions,
        referenced=referenced,
    )
    return list(resolved.recipients) if resolved is not None else None


async def slowmode_retry_after_ms(redis: Redis, key: str) -> int:
    remaining = await redis.pttl(key)
    return max(1000, int(remaining) if isinstance(remaining, int) else 1000)


async def require_dm_send(session: AsyncSession, access: ChannelAccess, actor: User) -> None:
    if access.guild is not None:
        return
    conversation = await session.get(
        DMConversation, (access.channel.id, access.channel.origin_domain)
    )
    if conversation is not None and conversation.type == "group":
        return
    for participant in access.participants:
        if (participant.id, participant.origin_domain) != (actor.id, actor.origin_domain):
            await lock_relationship_pair(session, actor, participant)
            if await blocked_between(session, actor, participant):
                raise HTTPException(status_code=403, detail={"code": "DM_PRIVACY_REJECTED"})
            if not participant.is_local:
                # The recipient's home instance revalidates its current policy
                # while ingesting the signed event.  Local replicated blocks
                # are still enforced synchronously above.
                continue
            await require_can_direct_message(session, actor, participant)


async def queue_attachment_tombstones(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    _actor: User,
    messages: list[Message],
) -> tuple[list[Attachment], set[str]]:
    refs = {(message.id, message.origin_domain) for message in messages}
    by_message = await attachments_for_messages(session, refs)
    attachments = [item for rows in by_message.values() for item in rows]
    if not attachments:
        return [], set()
    destinations: set[str] = set()
    local_attachments: list[Attachment] = []
    messages_by_ref = {(message.id, message.origin_domain): message for message in messages}
    for attachment in sorted(attachments, key=lambda item: (item.origin_domain, item.id)):
        if attachment.message_id is None or attachment.message_domain is None:
            raise RuntimeError("attachment deletion lost its authoritative message binding")
        message_ref = (attachment.message_id, attachment.message_domain)
        message = messages_by_ref.get(message_ref)
        if message is None or message.deleted_at is None:
            raise RuntimeError("attachment deletion lost its authoritative message")
        if attachment.origin_domain == settings.domain:
            local_attachments.append(attachment)
            destinations.update(
                await queue_terminal_attachment_tombstone(session, settings, attachment)
            )
        elif access.guild is not None and access.guild.origin_domain == settings.domain:
            destination = await queue_guild_media_delete_request(
                session,
                settings,
                guild=access.guild,
                message=message,
                attachment=attachment,
                deleted_at=message.deleted_at,
            )
            if destination is not None:
                destinations.add(destination)
    return local_attachments, destinations


def require_local_mutation_authority(access: ChannelAccess, settings: Settings) -> None:
    remote_guild = access.guild is not None and access.guild.origin_domain != settings.domain
    # A federated DM is authoritative where the conversation/channel was
    # minted, not wherever every participant happens to live.  Remote users are
    # expected in an authoritative DM and must not make local authority fail.
    remote_dm = access.guild is None and access.channel.origin_domain != settings.domain
    if remote_guild or remote_dm:
        raise RuntimeError("message mutation reached a non-authoritative local branch")


async def proxy_remote_guild_pin(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    message_ref: EntityReferenceLike,
    *,
    pinned: bool,
    reason: str | None = None,
) -> Response:
    guild = access.guild
    if guild is None or guild.origin_domain == settings.domain:
        raise RuntimeError("pin proxy requires a remote guild")
    message_id, message_domain = message_ref.resolve(settings.domain)
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/proxy-pin",
            payload={
                "actor": profile_from_user(actor),
                "channel_id": str(access.channel.id),
                "message_id": f"{message_id}@{message_domain}",
                "pinned": pinned,
                "reason": normalize_audit_reason(reason),
            },
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"}
        ) from None
    raise_proxy_rejection(response, {400, 403, 404, 409, 429, 507})
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"})
    try:
        body = decode_federation_response_json(response)
    except FederationNetworkError:
        body = None
    try:
        validate_federated_boolean_ack(body, "pinned", pinned)
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
        ) from None
    return Response(status_code=204)


async def proxy_remote_channel_pins(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    *,
    before: datetime | None,
    limit: int,
) -> dict[str, object]:
    """Read a complete pin page from the channel's single authority."""

    guild = access.guild
    authority = guild.origin_domain if guild is not None else access.channel.origin_domain
    if authority == settings.domain:
        raise RuntimeError("pin page proxy requires a remote authority")
    path = (
        f"/_kaede/v1/guilds/{guild.id}/pins"
        if guild is not None
        else f"/_kaede/v1/dms/{access.channel.id}/pins"
    )
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            authority,
            path,
            payload={
                "actor": profile_from_user(actor),
                "channel_id": str(access.channel.id),
                "before": before.isoformat() if before is not None else None,
                "limit": limit,
            },
            max_response_bytes=4 * 1024 * 1024,
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_PINS_UNAVAILABLE"},
        ) from None
    raise_proxy_rejection(response, {400, 403, 404, 422, 429, 507})
    if response.status_code != 200:
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_PINS_UNAVAILABLE"},
        )
    try:
        body = decode_federation_response_json(response, max_response_bytes=4 * 1024 * 1024)
        return validate_pin_page_payload(
            body,
            channel_ref=(access.channel.id, access.channel.origin_domain),
            limit=limit,
            before=before,
        )
    except (FederationNetworkError, ValueError):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_PINS_RESPONSE_INVALID"},
        ) from None


@dataclass(frozen=True, slots=True)
class FederatedEditAttachmentTransport:
    refs: tuple[tuple[int, str], ...] = ()
    attachments: tuple[dict[str, object], ...] = ()


async def prepare_federated_edit_attachments(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    message_ref: EntityReferenceLike,
    edit: MessageEdit | None,
    options: MessageMutationOptions | None,
    *,
    destination: str,
) -> FederatedEditAttachmentTransport:
    """Finalize and disclose only the new files in a remote message edit.

    The edit body keeps Discord's numeric attachment IDs.  This companion
    transport qualifies every retained ID and carries immutable metadata only
    for sender-home uploads the authority has not seen yet.
    """

    if edit is None or edit.attachment_ids is None:
        return FederatedEditAttachmentTransport()
    mutation_options = options or default_message_mutation_options()
    message = await channel_message(
        session,
        settings,
        access.channel,
        message_ref,
        require_active=True,
    )
    current = list(
        await session.scalars(
            select(Attachment).where(
                Attachment.message_id == message.id,
                Attachment.message_domain == message.origin_domain,
                Attachment.deleted_at.is_(None),
            )
        )
    )
    current_by_id: dict[int, list[Attachment]] = {}
    for attachment in current:
        current_by_id.setdefault(attachment.id, []).append(attachment)

    refs: list[tuple[int, str]] = []
    new_attachments: list[Attachment] = []
    for raw_id in edit.attachment_ids:
        attachment_id = int(raw_id)
        retained = current_by_id.get(attachment_id, [])
        if len(retained) > 1:
            raise HTTPException(
                status_code=409,
                detail={"code": "ATTACHMENT_REFERENCE_AMBIGUOUS"},
            )
        if retained:
            attachment = retained[0]
        else:
            await lock_media_tombstone_ref(session, attachment_id, settings.domain)
            attachment = await finalize_attachment(
                session,
                settings,
                actor,
                attachment_id,
                required_purpose="attachment",
            )
            require_available_edit_attachment(attachment, access.channel, mutation_options)
            new_attachments.append(attachment)
        refs.append((attachment.id, attachment.origin_domain))

    if new_attachments:
        room_ref = (
            ("guild", access.guild.id, access.guild.origin_domain)
            if access.guild is not None
            else None
        )
        await record_attachment_recipients(
            session,
            {(item.id, item.origin_domain) for item in new_attachments},
            destination,
            room_ref=room_ref,
        )
        await session.commit()
        for attachment in new_attachments:
            await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
    return FederatedEditAttachmentTransport(
        refs=tuple(refs),
        attachments=tuple(attachment_payload(item) for item in new_attachments),
    )


async def proxy_remote_guild_message_operation(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    operation: str,
    *,
    message_ref: EntityReferenceLike | None = None,
    message_refs: Sequence[EntityReferenceLike] = (),
    edit: MessageEdit | None = None,
    emoji: str | None = None,
    target_user_ref: EntityReferenceLike | None = None,
    mutation_options: MessageMutationOptions | None = None,
    redis: Redis | None = None,
) -> dict[str, object]:
    guild = access.guild
    if guild is None or guild.origin_domain == settings.domain:
        raise RuntimeError("message operation proxy requires a remote guild")
    attachment_transport = (
        await prepare_federated_edit_attachments(
            session,
            settings,
            access,
            actor,
            message_ref,
            edit,
            mutation_options,
            destination=guild.origin_domain,
        )
        if message_ref is not None
        else FederatedEditAttachmentTransport()
    )
    expression_authorizations: dict[str, dict[str, object]] = {}
    expression_sticker_items: list[dict[str, object]] = []
    if operation == "message.edit" and edit is not None:
        if redis is None:
            raise RuntimeError("federated expression edit requires Redis")
        raw_sticker_refs = (
            edit.e2ee.get("message_sticker_refs", [])
            if "e2ee" in edit.model_fields_set
            and isinstance(edit.e2ee, dict)
            and "rich_payload_digest" in edit.e2ee
            else []
        )
        if not isinstance(raw_sticker_refs, list):
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
            )
        try:
            edit_sticker_refs = [EntityRef(str(item)) for item in raw_sticker_refs]
            edit_expression_tokens = expression_custom_emoji_tokens(
                content=(edit.content if "content" in edit.model_fields_set else None),
                components=(edit.components if "components" in edit.model_fields_set else None),
                poll=None,
                e2ee=(edit.e2ee if "e2ee" in edit.model_fields_set else None),
                default_domain=guild.origin_domain,
            )
            if edit_expression_tokens or edit_sticker_refs or edit.expression_actor_intents:
                if message_ref is None:
                    raise ValueError("expression edit requires a message")
                message_id, message_domain = message_ref.resolve(settings.domain)
                application_ref = (
                    (mutation_options.application_id, mutation_options.application_domain)
                    if mutation_options is not None
                    and mutation_options.application_id is not None
                    and mutation_options.application_domain is not None
                    else await expression_application_ref_for_actor(session, actor)
                )
                operation_id = hashlib.sha256(
                    f"message.edit\n{message_id}@{message_domain}".encode()
                ).hexdigest()
                (
                    expression_authorizations,
                    expression_sticker_items,
                ) = await acquire_expression_use_authorizations(
                    session,
                    redis,
                    settings,
                    actor,
                    application_ref=application_ref,
                    actor_intents=edit.expression_actor_intents,
                    target_guild_ref=f"{guild.id}@{guild.origin_domain}",
                    target_channel_ref=(f"{access.channel.id}@{access.channel.origin_domain}"),
                    target_message_ref=f"{message_id}@{message_domain}",
                    operation="message.edit",
                    operation_id=operation_id,
                    emoji_tokens=edit_expression_tokens,
                    sticker_refs=edit_sticker_refs,
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            ) from exc
    body: dict[str, object] = {
        "operation": operation,
        "actor": profile_from_user(actor),
        "channel_id": str(access.channel.id),
        "message_id": (
            str(message_ref)
            if message_ref is not None and "@" in str(message_ref)
            else (
                f"{message_ref.resolve(settings.domain)[0]}@{message_ref.resolve(settings.domain)[1]}"
                if message_ref is not None
                else None
            )
        ),
        "message_ids": [
            f"{item.resolve(settings.domain)[0]}@{item.resolve(settings.domain)[1]}"
            for item in message_refs
        ],
        "edit": (
            edit.model_dump(
                mode="json",
                exclude_unset=True,
                exclude={"expression_actor_intents"},
            )
            if edit is not None
            else None
        ),
        "emoji": emoji,
        "target_user_id": (
            f"{target_user_ref.resolve(settings.domain)[0]}@"
            f"{target_user_ref.resolve(settings.domain)[1]}"
            if target_user_ref is not None
            else None
        ),
        "application_id": (
            f"{mutation_options.application_id}@{mutation_options.application_domain}"
            if mutation_options is not None
            and mutation_options.application_id is not None
            and mutation_options.application_domain is not None
            else None
        ),
        "authoritative_mention_user_ids": [
            f"{user_id}@{domain}"
            for user_id, domain in (
                mutation_options.authoritative_mention_refs
                if mutation_options is not None
                and mutation_options.authoritative_mention_refs is not None
                else ()
            )
        ],
        "attachment_refs": [
            f"{attachment_id}@{attachment_domain}"
            for attachment_id, attachment_domain in attachment_transport.refs
        ],
        "attachments": list(attachment_transport.attachments),
        "expression_authorizations": expression_authorizations,
        "expression_sticker_items": expression_sticker_items,
    }
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/message-operation",
            payload=body,
            max_response_bytes=2 * 1024 * 1024,
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_WRITE_UNAVAILABLE"},
        ) from None
    raise_proxy_rejection(response, {400, 403, 404, 409, 413, 422, 429, 507})
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"})
    try:
        result = decode_federation_response_json(response, max_response_bytes=2 * 1024 * 1024)
    except FederationNetworkError:
        result = None
    try:
        message_key = message_ref.resolve(settings.domain) if message_ref is not None else None
        result = validate_federated_message_operation_response(
            result,
            operation=operation,
            channel_ref=(access.channel.id, access.channel.origin_domain),
            message_ref=message_key,
        )
    except (FederationNetworkError, TypeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
        ) from None
    return result


async def proxy_remote_dm_message_operation(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    operation: str,
    message_ref: EntityReferenceLike,
    *,
    edit: MessageEdit | None = None,
    emoji: str | None = None,
    answer_id: int | None = None,
    after: EntityReferenceLike | None = None,
    limit: int = 50,
    mutation_options: MessageMutationOptions | None = None,
) -> dict[str, object]:
    if access.guild is not None or access.channel.origin_domain == settings.domain:
        raise RuntimeError("DM message operation proxy requires a remote authority")
    message_id, message_domain = message_ref.resolve(settings.domain)
    after_ref = after.resolve(settings.domain) if after is not None else None
    attachment_transport = await prepare_federated_edit_attachments(
        session,
        settings,
        access,
        actor,
        message_ref,
        edit,
        mutation_options,
        destination=access.channel.origin_domain,
    )
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            access.channel.origin_domain,
            f"/_kaede/v1/dms/{access.channel.id}/message-operation",
            payload={
                "operation": operation,
                "actor": {"id": str(actor.id), "domain": actor.origin_domain},
                "message_id": f"{message_id}@{message_domain}",
                "edit": edit.model_dump(mode="json", exclude_unset=True)
                if edit is not None
                else None,
                "emoji": emoji,
                "answer_id": answer_id,
                "after": (f"{after_ref[0]}@{after_ref[1]}" if after_ref is not None else None),
                "limit": limit,
                "application_id": (
                    f"{mutation_options.application_id}@{mutation_options.application_domain}"
                    if mutation_options is not None
                    and mutation_options.application_id is not None
                    and mutation_options.application_domain is not None
                    else None
                ),
                "authoritative_mention_user_ids": [
                    f"{user_id}@{domain}"
                    for user_id, domain in (
                        mutation_options.authoritative_mention_refs
                        if mutation_options is not None
                        and mutation_options.authoritative_mention_refs is not None
                        else ()
                    )
                ],
                "attachment_refs": [
                    f"{attachment_id}@{attachment_domain}"
                    for attachment_id, attachment_domain in attachment_transport.refs
                ],
                "attachments": list(attachment_transport.attachments),
            },
            max_response_bytes=2 * 1024 * 1024,
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_WRITE_UNAVAILABLE"},
        ) from None
    raise_proxy_rejection(response, {400, 403, 404, 409, 413, 422, 429, 507})
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"})
    try:
        result = decode_federation_response_json(
            response,
            max_response_bytes=2 * 1024 * 1024,
        )
    except FederationNetworkError:
        result = None
    try:
        result = validate_federated_message_operation_response(
            result,
            operation=operation,
            channel_ref=(access.channel.id, access.channel.origin_domain),
            message_ref=(message_id, message_domain),
            after=after_ref,
            limit=limit,
        )
    except (FederationNetworkError, TypeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
        ) from None
    return result


async def proxy_remote_guild_reaction(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    message_ref: EntityReferenceLike,
    emoji: str,
    *,
    remove: bool,
    expression_authorizations: dict[str, dict[str, object]] | None = None,
    application_ref: tuple[int, str] | None = None,
) -> Response:
    guild = access.guild
    if guild is None or guild.origin_domain == settings.domain:
        raise RuntimeError("reaction proxy requires a remote guild")
    message_id, message_domain = message_ref.resolve(settings.domain)
    payload: dict[str, object] = {
        "actor": profile_from_user(actor),
        "channel_id": str(access.channel.id),
        "message_id": f"{message_id}@{message_domain}",
        "emoji": emoji,
        "remove": remove,
    }
    if expression_authorizations:
        payload["expression_authorizations"] = expression_authorizations
    if application_ref is not None:
        payload["application_id"] = f"{application_ref[0]}@{application_ref[1]}"
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/proxy-reaction",
            payload=payload,
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"}
        ) from None
    raise_proxy_rejection(response, {400, 403, 404, 409, 429, 507})
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"})
    try:
        body = decode_federation_response_json(response)
    except FederationNetworkError:
        body = None
    try:
        validate_federated_boolean_ack(body, "reacted", not remove)
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
        ) from None
    return Response(status_code=204)


async def expression_application_ref_for_actor(
    session: AsyncSession,
    actor: User,
) -> tuple[int, str] | None:
    if actor.account_type != "bot":
        return None
    application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.bot_user_id == actor.id,
            BotApplication.bot_user_domain == actor.origin_domain,
            BotApplication.status == "active",
        )
        .limit(1)
    )
    if application is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    return application.id, application.origin_domain


async def prepare_reaction_expression_authorizations(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    message_ref: EntityReferenceLike,
    emoji: str,
    actor_intents: dict[str, dict[str, object]],
    actor_permissions: int,
) -> tuple[dict[str, dict[str, object]], tuple[int, str] | None]:
    guild = access.guild
    if guild is None:
        if actor_intents:
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            )
        return {}, None
    expression_tokens = [emoji] if custom_emoji_refs(emoji) else []
    if not expression_tokens and not actor_intents:
        return {}, None
    message_id, message_domain = message_ref.resolve(settings.domain)
    operation_id = hashlib.sha256(
        f"reaction.add\n{message_id}@{message_domain}\n{emoji}".encode()
    ).hexdigest()
    application_ref = await expression_application_ref_for_actor(session, actor)
    try:
        proofs, sticker_items = await acquire_expression_use_authorizations(
            session,
            redis,
            settings,
            actor,
            application_ref=application_ref,
            actor_intents=actor_intents,
            target_guild_ref=f"{guild.id}@{guild.origin_domain}",
            target_channel_ref=f"{access.channel.id}@{access.channel.origin_domain}",
            target_message_ref=f"{message_id}@{message_domain}",
            operation="reaction.add",
            operation_id=operation_id,
            emoji_tokens=expression_tokens,
            sticker_refs=[],
        )
        if sticker_items:
            raise ValueError("reaction expression authorization returned stickers")
        if guild.origin_domain == settings.domain:
            attested_tokens, attested_items = await validate_expression_authorization_map(
                session,
                redis,
                settings,
                proofs,
                requester_ref=f"{actor.id}@{actor.origin_domain}",
                requester_type=cast(Literal["human", "bot"], actor.account_type),
                application_ref=(
                    f"{application_ref[0]}@{application_ref[1]}"
                    if application_ref is not None
                    else None
                ),
                target_guild_ref=f"{guild.id}@{guild.origin_domain}",
                target_channel_ref=f"{access.channel.id}@{access.channel.origin_domain}",
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
        return proofs, application_ref
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
        ) from exc


async def proxy_remote_guild_poll_vote(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    message_ref: EntityReferenceLike,
    answer_id: int,
    *,
    remove: bool,
) -> Response:
    guild = access.guild
    if guild is None or guild.origin_domain == settings.domain:
        raise RuntimeError("poll proxy requires a remote guild")
    message_id, message_domain = message_ref.resolve(settings.domain)
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/proxy-poll-vote",
            payload={
                "actor": profile_from_user(actor),
                "channel_id": str(access.channel.id),
                "message_id": f"{message_id}@{message_domain}",
                "answer_id": answer_id,
                "remove": remove,
            },
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"}
        ) from None
    raise_proxy_rejection(response, {400, 403, 404, 409, 429, 507})
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"})
    try:
        body = decode_federation_response_json(response)
    except FederationNetworkError:
        body = None
    try:
        validate_federated_boolean_ack(body, "voted", not remove)
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
        ) from None
    return Response(status_code=204)


async def proxy_remote_guild_poll_finalize(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    message_ref: EntityReferenceLike,
) -> dict[str, object]:
    guild = access.guild
    if guild is None or guild.origin_domain == settings.domain:
        raise RuntimeError("poll proxy requires a remote guild")
    message_id, message_domain = message_ref.resolve(settings.domain)
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/proxy-poll-finalize",
            payload={
                "actor": profile_from_user(actor),
                "channel_id": str(access.channel.id),
                "message_id": f"{message_id}@{message_domain}",
            },
            max_response_bytes=512 * 1024,
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"}
        ) from None
    raise_proxy_rejection(response, {400, 403, 404, 409, 429, 507})
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"})
    try:
        body = decode_federation_response_json(response, max_response_bytes=512 * 1024)
    except FederationNetworkError:
        body = None
    remote_poll = body.get("poll") if isinstance(body, dict) else None
    remote_results = remote_poll.get("results") if isinstance(remote_poll, dict) else None
    if (
        not isinstance(body, dict)
        or body.get("id") != str(message_id)
        or body.get("origin_domain") != message_domain
        or body.get("channel_id") != str(access.channel.id)
        or body.get("channel_domain") != access.channel.origin_domain
        or not isinstance(remote_results, dict)
        or remote_results.get("is_finalized") is not True
    ):
        raise HTTPException(status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"})
    return {str(key): value for key, value in body.items()}


async def proxy_remote_guild_poll_voters(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    message_ref: EntityReferenceLike,
    answer_id: int,
    *,
    after: EntityRef | None,
    limit: int,
) -> dict[str, object]:
    guild = access.guild
    if guild is None or guild.origin_domain == settings.domain:
        raise RuntimeError("poll proxy requires a remote guild")
    message_id, message_domain = message_ref.resolve(settings.domain)
    after_ref = after.resolve(settings.domain) if after is not None else None
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/proxy-poll-voters",
            payload={
                "actor": profile_from_user(actor),
                "channel_id": str(access.channel.id),
                "message_id": f"{message_id}@{message_domain}",
                "answer_id": answer_id,
                "after": (f"{after_ref[0]}@{after_ref[1]}" if after_ref is not None else None),
                "limit": limit,
            },
            max_response_bytes=256 * 1024,
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503, detail={"code": "FEDERATED_READ_UNAVAILABLE"}
        ) from None
    raise_proxy_rejection(response, {400, 403, 404, 409, 429})
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "FEDERATED_READ_UNAVAILABLE"})
    try:
        body = decode_federation_response_json(response, max_response_bytes=256 * 1024)
    except FederationNetworkError:
        body = None
    try:
        body = validate_federated_user_page(
            body,
            collection="users",
            after=after_ref,
            limit=limit,
            include_total=False,
        )
    except (FederationNetworkError, TypeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_READ_RESPONSE_INVALID"},
        ) from None
    return body


async def channel_message(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
    message_ref: EntityReferenceLike,
    *,
    for_update: bool = False,
    require_active: bool = False,
) -> Message:
    message_id, message_domain = message_ref.resolve(settings.domain)
    statement = select(Message).where(
        Message.id == message_id,
        Message.origin_domain == message_domain,
        Message.channel_id == channel.id,
        Message.channel_domain == channel.origin_domain,
    )
    if for_update:
        statement = statement.with_for_update()
    message = await session.scalar(statement)
    if message is None or (require_active and message.deleted_at is not None):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    return message


async def dm_delivery_statuses(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
    messages: list[Message],
) -> dict[tuple[int, str], tuple[str, str | None]]:
    """Reconstruct sender-side federated DM delivery state from durable outbox rows."""
    local_ids = [
        str(message.id) for message in messages if message.author_domain == settings.domain
    ]
    if not local_ids:
        return {}
    rows = (
        await session.execute(
            select(
                FederationEvent.envelope,
                FederationOutbox.status,
                FederationOutbox.last_error,
            )
            .join(
                FederationOutbox,
                (FederationOutbox.event_origin_domain == FederationEvent.origin_domain)
                & (FederationOutbox.event_id == FederationEvent.event_id),
            )
            .where(
                FederationEvent.origin_domain == settings.domain,
                FederationEvent.event_type.in_(("dm.message.create", "dm.group.message.proposed")),
                FederationEvent.envelope["content"]["message"]["channel_id"].astext
                == str(channel.id),
                FederationEvent.envelope["content"]["message"]["id"].astext.in_(local_ids),
            )
        )
    ).all()
    by_message: dict[tuple[int, str], list[tuple[str, str | None]]] = {}
    for envelope, status_value, last_error in rows:
        message = envelope.get("content", {}).get("message", {})
        try:
            reference = (int(message["id"]), str(message["origin_domain"]))
        except (KeyError, TypeError, ValueError):
            continue
        by_message.setdefault(reference, []).append(
            (str(status_value), str(last_error) if last_error is not None else None)
        )
    result: dict[tuple[int, str], tuple[str, str | None]] = {}
    for reference, attempts in by_message.items():
        statuses = [item[0] for item in attempts]
        if any(value in {"failed", "expired"} for value in statuses):
            code = next((item[1] for item in attempts if item[1]), None)
            result[reference] = ("failed", code)
        elif all(value == "delivered" for value in statuses):
            result[reference] = ("delivered", None)
        elif any(value in {"retry", "circuit"} for value in statuses):
            code = next((item[1] for item in attempts if item[1]), None)
            result[reference] = ("retrying", code)
        else:
            result[reference] = ("pending", None)
    return result


@router.post(
    "/{channel_id}/messages/{message_id}/forward/prepare",
)
async def prepare_forward_message(
    channel_id: EntityRef,
    message_id: EntityRef,
    payload: MessageForwardPrepare,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Authorize and pin one source projection for each chosen destination."""

    source_access = await load_channel_access(session, settings, auth.user, channel_id)
    await require_channel_permissions(
        session,
        redis,
        source_access,
        auth.user,
        required_permissions("message.list"),
    )
    source = await channel_message(
        session,
        settings,
        source_access.channel,
        message_id,
        require_active=True,
    )
    if (
        await session.get(Poll, (source.id, source.origin_domain)) is not None
        or source.message_type not in FORWARDABLE_MESSAGE_TYPES
    ):
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_NOT_FORWARDABLE"})
    source_attachments = list(
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
    prepared: list[dict[str, object]] = []
    source_projection: dict[str, object] | None = None
    for requested in payload.destinations:
        destination_access = await load_channel_access(
            session,
            settings,
            auth.user,
            requested.channel_id,
        )
        needed = (
            Permission.SEND_MESSAGES_IN_THREADS
            if destination_access.channel.type in THREAD_CHANNEL_TYPES
            else Permission.SEND_MESSAGES
        )
        await require_channel_permissions(
            session,
            redis,
            destination_access,
            auth.user,
            Permission.VIEW_CHANNEL | needed,
        )
        if source_access.channel.origin_domain == settings.domain:
            source_nsfw = await effective_channel_nsfw(session, source_access.channel)
            if source_nsfw is None:
                raise _forward_proof_http_error("FORWARD_CONTEXT_UNSUPPORTED")
            authorization = await local_forward_source_proof(
                session,
                settings,
                requester=auth.user,
                source=source,
                source_channel=source_access.channel,
                destination_channel=destination_access.channel,
                attachments=source_attachments,
                source_nsfw=source_nsfw,
                nonce=requested.client_nonce,
                application_ref=None,
                e2ee_device_id=None,
            )
        else:
            authorization = await remote_forward_source_proof(
                session,
                settings,
                requester=auth.user,
                source_message_ref=(source.id, source.origin_domain),
                source_channel=source_access.channel,
                destination_channel=destination_access.channel,
                nonce=requested.client_nonce,
            )
        proof = await validate_signed_forward_source_proof(
            session,
            settings,
            authorization,
            requester=auth.user,
            source_message_ref=(source.id, source.origin_domain),
            source_channel_ref=(source.channel_id, source.channel_domain),
            destination_channel=destination_access.channel,
            nonce=requested.client_nonce,
            application_ref=None,
            e2ee_device_id=None,
        )
        projection = {
            "message_ref": proof["source_message_ref"],
            "channel_ref": proof["source_channel_ref"],
            "encryption_mode": proof["source_encryption_mode"],
            "projection_version": proof["source_projection_version"],
            "projection_digest": proof["source_projection_digest"],
            "created_at": proof["source_created_at"],
            "edited_at": proof["source_edited_at"],
            "flags": proof["source_flags"],
            "message_type": proof["source_message_type"],
            "nsfw": proof["source_nsfw"],
            "attachment_refs": proof["source_attachment_refs"],
            "snapshot": proof["source_snapshot"],
        }
        if source_projection is None:
            source_projection = projection
        elif source_projection != projection:
            raise _forward_proof_http_error()
        prepared.append(
            {
                "channel_id": (
                    f"{destination_access.channel.id}@{destination_access.channel.origin_domain}"
                ),
                "client_nonce": requested.client_nonce,
                "encryption_mode": destination_access.channel.encryption_mode,
                "requires_plaintext_disclosure": (
                    proof["source_encryption_mode"] == "e2ee"
                    and destination_access.channel.encryption_mode == "plaintext"
                ),
                "authorization": authorization,
            }
        )
    if source_projection is None:
        raise RuntimeError("forward preparation produced no destinations")
    return {"source": source_projection, "destinations": prepared}


@router.post(
    "/{channel_id}/messages/{message_id}/forward",
    status_code=status.HTTP_201_CREATED,
)
async def forward_message(
    channel_id: EntityRef,
    message_id: EntityRef,
    payload: MessageForwardCreate,
    response_status: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Forward one immutable snapshot to as many as five destinations."""

    source_access = await load_channel_access(session, settings, auth.user, channel_id)
    await require_channel_permissions(
        session,
        redis,
        source_access,
        auth.user,
        required_permissions("message.list"),
    )
    source = await channel_message(
        session,
        settings,
        source_access.channel,
        message_id,
        require_active=True,
    )
    results: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    requested_destinations = (
        [(item.destination_channel_id, item.message) for item in payload.destinations]
        if payload.destinations
        else [
            (
                destination,
                MessageCreate(
                    content=payload.content,
                    forwarded_message_id=EntityRef(f"{source.id}@{source.origin_domain}"),
                    client_nonce=f"forward-{source.id}-{index}-{secrets.token_hex(8)}",
                ),
            )
            for index, destination in enumerate(payload.destination_channel_ids)
        ]
    )
    for destination, message_payload_create in requested_destinations:
        if (
            message_payload_create.forwarded_message_id is None
            or message_payload_create.forwarded_message_id.resolve(settings.domain)
            != (source.id, source.origin_domain)
            or (payload.destinations and message_payload_create.forward_source_proof is None)
        ):
            failures.append(
                {
                    "destination_channel_ref": str(destination),
                    "status": 409,
                    "error": {"code": "FORWARD_SOURCE_PROOF_INVALID"},
                }
            )
            continue
        try:
            message = await create_message(
                channel_id=destination,
                payload=message_payload_create,
                response_status=Response(),
                auth=auth,
                session=session,
                redis=redis,
                snowflake=snowflake,
                settings=settings,
                admission_options=MessageAdmissionOptions(),
            )
        except HTTPException as exc:
            await session.rollback()
            detail = exc.detail if isinstance(exc.detail, dict) else {"code": "FORWARD_FAILED"}
            failures.append(
                {
                    "destination_channel_ref": str(destination),
                    "status": exc.status_code,
                    "error": detail,
                }
            )
            continue
        results.append(
            {
                "destination_channel_ref": str(destination),
                "message": message,
            }
        )
    if failures:
        response_status.status_code = status.HTTP_207_MULTI_STATUS
    return {"forwards": results, "failures": failures}


@router.get("/{channel_id}/messages")
async def list_messages(
    channel_id: EntityRef,
    before: EntityRef | None = None,
    after: EntityRef | None = None,
    around: EntityRef | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    if sum(value is not None for value in (before, after, around)) > 1:
        raise HTTPException(status_code=400, detail={"code": "INVALID_PAGINATION"})
    access = await load_channel_access(session, settings, auth.user, channel_id)
    channel = access.channel
    await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("message.list"),
    )
    conditions = [
        Message.channel_id == channel.id,
        Message.channel_domain == channel.origin_domain,
        Message.id >= channel.created_floor_id,
    ]
    if before is not None:
        before_id, before_domain = before.resolve(settings.domain)
        conditions.append(tuple_(Message.id, Message.origin_domain) < (before_id, before_domain))
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
        conditions.append(tuple_(Message.id, Message.origin_domain) > (after_id, after_domain))
    if around is not None:
        around_id, around_domain = around.resolve(settings.domain)
        newer_limit = (limit + 1) // 2
        older_limit = limit - newer_limit
        newer = list(
            await session.scalars(
                select(Message)
                .where(
                    *conditions,
                    tuple_(Message.id, Message.origin_domain) >= (around_id, around_domain),
                )
                .order_by(Message.id.asc(), Message.origin_domain.asc())
                .limit(newer_limit)
            )
        )
        older = list(
            await session.scalars(
                select(Message)
                .where(
                    *conditions,
                    tuple_(Message.id, Message.origin_domain) < (around_id, around_domain),
                )
                .order_by(Message.id.desc(), Message.origin_domain.desc())
                .limit(older_limit)
            )
        )
        messages = sorted(
            [*older, *newer],
            key=lambda item: (item.id, item.origin_domain),
            reverse=True,
        )
    else:
        order = (
            (Message.id.asc(), Message.origin_domain.asc())
            if after is not None
            else (Message.id.desc(), Message.origin_domain.desc())
        )
        messages = list(
            await session.scalars(select(Message).where(*conditions).order_by(*order).limit(limit))
        )
        if after is not None:
            messages.reverse()
    parent_starter_ref: tuple[int, str] | None = None
    if (
        channel.type in THREAD_CHANNEL_TYPES
        and channel.starter_message_id == channel.id
        and channel.starter_message_domain == channel.origin_domain
        and channel.parent_id is not None
        and channel.parent_domain is not None
    ):
        parent_starter = await session.scalar(
            select(Message).where(
                Message.id == channel.starter_message_id,
                Message.origin_domain == channel.starter_message_domain,
                Message.channel_id == channel.parent_id,
                Message.channel_domain == channel.parent_domain,
            )
        )
        if parent_starter is not None:
            parent_starter_ref = (parent_starter.id, parent_starter.origin_domain)
            include_parent_starter = False
            if before is not None:
                include_parent_starter = parent_starter_ref < before.resolve(settings.domain)
            elif after is not None:
                include_parent_starter = parent_starter_ref > after.resolve(settings.domain)
            elif around is not None:
                around_ref = around.resolve(settings.domain)
                newer_limit = (limit + 1) // 2
                older_limit = limit - newer_limit
                if parent_starter_ref >= around_ref:
                    include_parent_starter = (
                        sum((item.id, item.origin_domain) >= around_ref for item in messages)
                        < newer_limit
                    )
                else:
                    include_parent_starter = (
                        sum((item.id, item.origin_domain) < around_ref for item in messages)
                        < older_limit
                    )
            else:
                include_parent_starter = len(messages) < limit
            if include_parent_starter:
                messages.append(parent_starter)
                messages.sort(
                    key=lambda item: (item.id, item.origin_domain),
                    reverse=True,
                )
    author_refs = {(item.author_id, item.author_domain) for item in messages}
    authors: dict[tuple[int, str], User] = {}
    if author_refs:
        users = await session.scalars(
            select(User).where(tuple_(User.id, User.origin_domain).in_(author_refs))
        )
        authors = {(user.id, user.origin_domain): user for user in users}
    attachments = await attachments_for_messages(
        session, {(item.id, item.origin_domain) for item in messages}
    )
    reaction_payloads = await reaction_payloads_for_messages(
        session,
        {(item.id, item.origin_domain) for item in messages},
        viewer=auth.user,
    )
    delivery_statuses = (
        await dm_delivery_statuses(session, settings, channel, messages)
        if access.guild is None
        else {}
    )
    payloads: list[dict[str, object]] = []
    for item in messages:
        reaction_counts, reacted_emoji = reaction_payloads.get(
            (item.id, item.origin_domain), ({}, [])
        )
        payload = message_payload(
            item,
            authors.get((item.author_id, item.author_domain)),
            attachments.get((item.id, item.origin_domain), []),
            poll=await render_poll_payload(session, item, viewer=auth.user),
        )
        source_starter_projection = parent_starter_ref == (item.id, item.origin_domain) and (
            item.channel_id,
            item.channel_domain,
        ) == (channel.parent_id, channel.parent_domain)
        if source_starter_projection:
            payload = thread_source_starter_payload(channel, payload)
        else:
            payload["reaction_counts"] = reaction_counts
            payload["reacted_emoji"] = reacted_emoji
        delivery = delivery_statuses.get((item.id, item.origin_domain))
        if delivery is not None:
            payload["delivery_status"] = delivery[0]
            if delivery[1] in {
                "FEDERATED_DM_STORAGE_QUOTA_EXCEEDED",
                "KAED_FED_DM_STORAGE_QUOTA_EXCEEDED",
            }:
                payload["delivery_error_code"] = delivery[1]
                payload["failure_reason"] = (
                    "The receiving instance is at capacity. Kaede is retrying automatically."
                )
        payloads.append(payload)
    # A non-authoritative DM keeps a bounded recent cache. Once pagination
    # reaches that cache's lower boundary, fill the remainder from the signed
    # authority without persisting another durable copy.
    conversation = (
        await session.get(DMConversation, (channel.id, channel.origin_domain))
        if access.guild is None
        else None
    )
    cache_start = (
        (
            conversation.history_cache_start_id,
            conversation.history_cache_start_domain,
        )
        if conversation is not None
        and conversation.history_cache_start_id is not None
        and conversation.history_cache_start_domain is not None
        else None
    )
    authority_history_available = await dm_authority_history_available(
        session, conversation, local_domain=settings.domain
    )
    should_fetch_remote = False
    remote_before: tuple[int, str] | None = None
    if (
        conversation is not None
        and conversation.authority_domain != settings.domain
        and conversation.history_truncated
        and authority_history_available
        and cache_start is not None
        and after is None
    ):
        requested_before = before.resolve(settings.domain) if before is not None else None
        requested_around = around.resolve(settings.domain) if around is not None else None
        around_is_local = requested_around is not None and any(
            (int(str(item["id"])), str(item["origin_domain"])) == requested_around
            for item in payloads
        )
        if requested_around is not None and not around_is_local:
            # The search authority may return an older result outside this
            # replica's rolling cache. Ask for the descending page immediately
            # after that snowflake so the exact target is included without
            # persisting another copy.
            if requested_around[0] < (1 << 63) - 1:
                should_fetch_remote = True
                remote_before = (
                    requested_around[0] + 1,
                    conversation.authority_domain,
                )
        elif payloads:
            local_oldest = (
                int(str(payloads[-1]["id"])),
                str(payloads[-1]["origin_domain"]),
            )
            if local_oldest <= cache_start:
                should_fetch_remote = True
                remote_before = requested_before
        else:
            if requested_before is None or requested_before <= cache_start:
                should_fetch_remote = True
                remote_before = requested_before
    if conversation is not None and should_fetch_remote:
        # Ask the authority for the complete logical page. Locally-authored
        # durable rows can be sparse below the rolling boundary; merely filling
        # the local remainder would otherwise create gaps or pagination loops.
        remote_limit = limit
        trusted_profiles = {
            (participant.id, participant.origin_domain): profile_from_user(participant)
            for participant in access.participants
        }
        participant_refs = set(trusted_profiles)
        try:
            remote_messages: list[dict[str, object]] = []
            remote_complete = False
            cursor = remote_before
            # Local-authored rows are deliberately ignored from the untrusted
            # authority body. Advance through a bounded number of signed pages
            # to fill the client page without permitting a cursor loop.
            for _ in range(4):
                remote_query = {
                    "limit": str(remote_limit),
                    "requester_id": str(auth.user.id),
                    "requester_domain": auth.user.origin_domain,
                }
                if cursor is not None:
                    remote_query.update(
                        {
                            "before_id": str(cursor[0]),
                            "before_domain": cursor[1],
                        }
                    )
                response = await signed_request(
                    session,
                    settings,
                    "GET",
                    conversation.authority_domain,
                    f"/_kaede/v1/dms/{conversation.id}/messages",
                    query=remote_query,
                    max_response_bytes=MAX_DM_HISTORY_RESPONSE_BYTES,
                )
                if conversation.type == "group":
                    await lock_terminal_room(
                        session,
                        "group_dm",
                        conversation.id,
                        conversation.origin_domain,
                    )
                    terminal_receipt = await session.get(
                        TerminalRoomDeletion,
                        (
                            "group_dm",
                            conversation.id,
                            conversation.origin_domain,
                            settings.domain,
                        ),
                    )
                    live_conversation = await session.get(
                        DMConversation,
                        (conversation.id, conversation.origin_domain),
                        populate_existing=True,
                    )
                    live_channel = await session.get(
                        Channel,
                        (conversation.id, conversation.origin_domain),
                        populate_existing=True,
                    )
                    live_participant = await session.get(
                        DMParticipant,
                        (
                            conversation.id,
                            conversation.origin_domain,
                            auth.user.id,
                            auth.user.origin_domain,
                        ),
                        populate_existing=True,
                    )
                    if (
                        terminal_receipt is not None
                        or live_conversation is None
                        or live_channel is None
                        or live_channel.unavailable
                        or live_participant is None
                    ):
                        raise HTTPException(
                            status_code=404,
                            detail={"code": "CHANNEL_NOT_FOUND"},
                        )
                    conversation = live_conversation
                if response.status_code != 200:
                    try:
                        retry_seconds = max(
                            1, min(3600, int(response.headers.get("Retry-After", "2")))
                        )
                    except ValueError:
                        retry_seconds = 2
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "FEDERATED_DM_HISTORY_UNAVAILABLE",
                            "retry_after_ms": retry_seconds * 1000,
                        },
                        headers={"Retry-After": str(retry_seconds)},
                    )
                body = decode_federation_response_json(
                    response,
                    max_response_bytes=MAX_DM_HISTORY_RESPONSE_BYTES,
                )
                remote_page = validate_dm_history_page(
                    body,
                    settings=settings,
                    conversation_ref=(conversation.id, conversation.origin_domain),
                    authority_domain=conversation.authority_domain,
                    participant_refs=participant_refs,
                    trusted_profiles=trusted_profiles,
                    before=cursor,
                    limit=remote_limit,
                )
                if remote_page.ignored_local_refs:
                    retained_local_refs = set(
                        (
                            await session.execute(
                                select(Message.id, Message.origin_domain).where(
                                    tuple_(Message.id, Message.origin_domain).in_(
                                        remote_page.ignored_local_refs
                                    ),
                                    Message.channel_id == conversation.id,
                                    Message.channel_domain == conversation.origin_domain,
                                    Message.author_domain == settings.domain,
                                )
                            )
                        ).tuples()
                    )
                    if retained_local_refs != set(remote_page.ignored_local_refs):
                        raise FederationNetworkError(
                            "DM history authority invented a locally-authored message"
                        )
                remote_messages.extend(remote_page.messages)
                remote_complete = remote_page.complete
                if remote_complete or remote_page.next_before is None:
                    break
                cursor = remote_page.next_before
                unique_refs = {
                    (str(item["id"]), str(item["origin_domain"]))
                    for item in [*payloads, *remote_messages]
                }
                if len(unique_refs) >= limit:
                    break
        except HTTPException as exc:
            if payloads:
                payloads[-1]["history_page_error_code"] = "FEDERATED_DM_HISTORY_UNAVAILABLE"
                detail: dict[str, object] = (
                    cast(dict[str, object], exc.detail) if isinstance(exc.detail, dict) else {}
                )
                retry_after_ms = detail.get("retry_after_ms")
                if isinstance(retry_after_ms, int):
                    payloads[-1]["history_page_retry_after_ms"] = retry_after_ms
                return payloads
            raise
        except (httpx.HTTPError, FederationNetworkError, RuntimeError):
            # The caller keeps its already-rendered cached messages and can
            # retry the same stable cursor. Never turn a temporary authority
            # outage into a false end-of-history marker.
            if payloads:
                payloads[-1]["history_page_error_code"] = "FEDERATED_DM_HISTORY_UNAVAILABLE"
                payloads[-1]["history_page_retry_after_ms"] = 2_000
                return payloads
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "FEDERATED_DM_HISTORY_UNAVAILABLE",
                    "retry_after_ms": 2_000,
                },
                headers={"Retry-After": "2"},
            ) from None
        payloads = merge_dm_history_messages(remote_messages, payloads, limit=limit)
        for payload_item in payloads:
            payload_item.pop("history_page_complete", None)
        local_has_more = False
        if payloads:
            oldest_merged = (
                int(str(payloads[-1]["id"])),
                str(payloads[-1]["origin_domain"]),
            )
            local_has_more = bool(
                await session.scalar(
                    select(
                        exists().where(
                            Message.channel_id == channel.id,
                            Message.channel_domain == channel.origin_domain,
                            Message.id >= channel.created_floor_id,
                            tuple_(Message.id, Message.origin_domain) < oldest_merged,
                        )
                    )
                )
            )
        if dm_history_page_is_complete(
            remote_complete=remote_complete,
            merged_messages=payloads,
            remote_messages=remote_messages,
            local_has_more=local_has_more,
        ):
            payloads[-1]["history_page_complete"] = True
    return payloads


@router.get("/{channel_id}/messages/{message_id}/forwarded")
async def resolve_forwarded_message(
    channel_id: EntityRef,
    message_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Resolve a live forward only after rechecking both channel grants."""

    destination_access = await load_channel_access(session, settings, auth.user, channel_id)
    await require_channel_permissions(
        session,
        redis,
        destination_access,
        auth.user,
        required_permissions("message.list"),
    )
    destination = await channel_message(
        session, settings, destination_access.channel, message_id, require_active=True
    )
    if destination.forwarded_message_id is None or destination.forwarded_message_domain is None:
        raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
    if destination.forward_snapshot is not None:
        if destination.forwarded_channel_id is None or destination.forwarded_channel_domain is None:
            raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
        source_access = await load_channel_access(
            session,
            settings,
            auth.user,
            EntityRef(f"{destination.forwarded_channel_id}@{destination.forwarded_channel_domain}"),
        )
        await require_channel_permissions(
            session,
            redis,
            source_access,
            auth.user,
            required_permissions("message.list"),
        )
        return {
            "source_channel_ref": (
                f"{destination.forwarded_channel_id}@{destination.forwarded_channel_domain}"
            ),
            "source_message_ref": (
                f"{destination.forwarded_message_id}@{destination.forwarded_message_domain}"
            ),
        }
    if (
        destination_access.guild is not None
        and destination_access.guild.origin_domain != settings.domain
    ):
        try:
            upstream = await signed_request(
                session,
                settings,
                "POST",
                destination_access.guild.origin_domain,
                f"/_kaede/v1/guilds/{destination_access.guild.id}/proxy-forward-resolve",
                payload={
                    "actor": profile_from_user(auth.user),
                    "channel_id": str(destination_access.channel.id),
                    "message_id": f"{destination.id}@{destination.origin_domain}",
                },
                request_timeout=10,
                max_response_bytes=512 * 1024,
            )
        except (httpx.HTTPError, FederationNetworkError, RuntimeError):
            raise HTTPException(
                status_code=503,
                detail={"code": "FEDERATED_FORWARD_UNAVAILABLE"},
            ) from None
        raise_proxy_rejection(upstream, {403, 404, 409, 429})
        if upstream.status_code != 200:
            raise HTTPException(
                status_code=503,
                detail={"code": "FEDERATED_FORWARD_UNAVAILABLE"},
            )
        try:
            remote = decode_federation_response_json(
                upstream,
                max_response_bytes=512 * 1024,
            )
        except FederationNetworkError:
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_FORWARD_RESPONSE_INVALID"},
            ) from None
        if (
            not isinstance(remote, dict)
            or remote.get("id") != str(destination.forwarded_message_id)
            or remote.get("origin_domain") != destination.forwarded_message_domain
            or not isinstance(remote.get("source_channel_ref"), str)
        ):
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_FORWARD_RESPONSE_INVALID"},
            )
        return {str(key): value for key, value in remote.items()}
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
        if follow is None:
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
                    "generation": str(follow.generation),
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
    if source is None and destination_access.guild is None:
        conversation = await session.get(
            DMConversation,
            (destination_access.channel.id, destination_access.channel.origin_domain),
        )
        if (
            conversation is not None
            and conversation.authority_domain != settings.domain
            and destination.forwarded_message_domain != settings.domain
        ):
            try:
                upstream = await signed_request(
                    session,
                    settings,
                    "POST",
                    conversation.authority_domain,
                    f"/_kaede/v1/dms/{conversation.id}/forward-resolve",
                    payload={
                        "requester": {
                            "id": str(auth.user.id),
                            "domain": auth.user.origin_domain,
                        },
                        "source_message_ref": (
                            f"{destination.forwarded_message_id}@"
                            f"{destination.forwarded_message_domain}"
                        ),
                    },
                    request_timeout=10,
                    max_response_bytes=MAX_DM_HISTORY_RESPONSE_BYTES,
                )
            except FederationNetworkError:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "FEDERATED_FORWARD_UNAVAILABLE"},
                ) from None
            if upstream.status_code == 404:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"},
                )
            if upstream.status_code != 200:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "FEDERATED_FORWARD_UNAVAILABLE"},
                )
            raw_source = decode_federation_response_json(
                upstream,
                max_response_bytes=MAX_DM_HISTORY_RESPONSE_BYTES,
            )
            participant_refs = {
                (participant.id, participant.origin_domain)
                for participant in destination_access.participants
            }
            trusted_profiles = {
                (participant.id, participant.origin_domain): profile_from_user(participant)
                for participant in destination_access.participants
            }
            try:
                page = validate_dm_history_page(
                    {
                        "conversation_id": str(conversation.id),
                        "conversation_domain": conversation.origin_domain,
                        "messages": [raw_source],
                        "next_before": None,
                        "complete": True,
                    },
                    settings=settings,
                    conversation_ref=(conversation.id, conversation.origin_domain),
                    authority_domain=conversation.authority_domain,
                    participant_refs=participant_refs,
                    trusted_profiles=trusted_profiles,
                    before=(destination.id, destination.origin_domain),
                    limit=1,
                )
            except FederationNetworkError as exc:
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATED_FORWARD_RESPONSE_INVALID"},
                ) from exc
            if (
                len(page.messages) != 1
                or page.messages[0].get("id") != str(destination.forwarded_message_id)
                or page.messages[0].get("origin_domain") != destination.forwarded_message_domain
            ):
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATED_FORWARD_RESPONSE_INVALID"},
                )
            rendered_remote = dict(page.messages[0])
            rendered_remote.pop("history_page_complete", None)
            rendered_remote["source_channel_ref"] = (
                f"{conversation.id}@{conversation.origin_domain}"
            )
            return rendered_remote
    if source is None or source.deleted_at is not None:
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
        source_access = await load_channel_access(
            session,
            settings,
            auth.user,
            EntityRef(f"{source.channel_id}@{source.channel_domain}"),
        )
        await require_channel_permissions(
            session,
            redis,
            source_access,
            auth.user,
            required_permissions("message.list"),
        )
    rendered = await render_message_payload(session, source, viewer=auth.user)
    rendered["source_channel_ref"] = f"{source.channel_id}@{source.channel_domain}"
    return rendered


async def load_message_create_access(
    session: AsyncSession,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_id: EntityRef,
    options: MessageAdmissionOptions,
) -> ChannelAccess:
    """Resolve the capability used to admit a message without widening it."""

    if options.poll_result is not None:
        return await load_poll_result_channel_access(session, settings, channel_id)
    if options.webhook_id is not None:
        if options.webhook_channel_id is None or options.webhook_channel_domain is None:
            raise RuntimeError("webhook message admission lost its bound channel")
        return await load_webhook_capability_channel_access(
            session,
            settings,
            channel_id,
            webhook_channel_id=options.webhook_channel_id,
            webhook_channel_domain=options.webhook_channel_domain,
        )
    if options.interaction_permissions is not None:
        return await load_interaction_permission_channel_access(
            session,
            settings,
            channel_id,
        )
    return await load_channel_access(session, settings, auth.user, channel_id)


async def lock_message_create_access(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    attachment_ids: Sequence[int],
) -> tuple[ChannelAccess, DMConversation | None]:
    """Take the room/media fences and refresh the projection they protect."""

    conversation = (
        await session.get(
            DMConversation,
            (access.channel.id, access.channel.origin_domain),
        )
        if access.guild is None
        else None
    )
    if access.guild is not None:
        await lock_terminal_room(
            session,
            "guild",
            access.guild.id,
            access.guild.origin_domain,
        )
    elif conversation is not None and conversation.type == "group":
        await lock_terminal_room(
            session,
            "group_dm",
            conversation.id,
            conversation.origin_domain,
        )
    if access.guild is not None:
        terminal_receipt = await session.get(
            TerminalRoomDeletion,
            (
                "guild",
                access.guild.id,
                access.guild.origin_domain,
                settings.domain,
            ),
        )
        refreshed_guild = await session.get(
            Guild,
            (access.guild.id, access.guild.origin_domain),
            populate_existing=True,
        )
        refreshed_channel = await session.get(
            Channel,
            (access.channel.id, access.channel.origin_domain),
            populate_existing=True,
        )
        if (
            terminal_receipt is not None
            or refreshed_guild is None
            or refreshed_guild.unavailable
            or refreshed_channel is None
            or refreshed_channel.unavailable
        ):
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        access = ChannelAccess(channel=refreshed_channel, guild=refreshed_guild, participants=[])
    elif conversation is not None and conversation.type == "group":
        terminal_receipt = await session.get(
            TerminalRoomDeletion,
            (
                "group_dm",
                conversation.id,
                conversation.origin_domain,
                settings.domain,
            ),
        )
        refreshed_conversation = await session.get(
            DMConversation,
            (conversation.id, conversation.origin_domain),
            populate_existing=True,
        )
        refreshed_channel = await session.get(
            Channel,
            (access.channel.id, access.channel.origin_domain),
            populate_existing=True,
        )
        if (
            terminal_receipt is not None
            or refreshed_conversation is None
            or refreshed_channel is None
            or refreshed_channel.unavailable
        ):
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        conversation = refreshed_conversation
        access = ChannelAccess(
            channel=refreshed_channel,
            guild=None,
            participants=access.participants,
        )

    # Message publication and PhotoDNA terminalization share this canonical
    # fence. Acquire it before finalize_attachment takes FOR UPDATE so a
    # verdict can never deadlock the sender or commit between finalization and
    # recipient-route insertion.
    for attachment_id in sorted({int(item) for item in attachment_ids}):
        await lock_media_tombstone_ref(session, attachment_id, settings.domain)
    return await lock_local_channel_mutation(session, settings, access), conversation


@dataclass(frozen=True, slots=True)
class MessageCreateMentions:
    """Stored mention intent and the exact recipients it may notify."""

    explicit_recipients: list[tuple[int, str]]
    recipients: list[tuple[int, str]]
    role_recipients: set[tuple[int, str]]
    roles: list[tuple[int, str]]
    everyone: bool


@dataclass(frozen=True, slots=True)
class MessageCreateExpressions:
    """Expression metadata admitted for a message and its federation proposal."""

    encrypted_rich: bool
    encrypted_custom_emoji_tokens: list[str]
    application_ref: tuple[int, str] | None
    authorizations: dict[str, dict[str, object]]
    sticker_items: list[dict[str, object]]


def encrypted_message_expression_refs(
    payload: MessageCreate,
) -> tuple[bool, list[EntityRef], list[str]]:
    envelope = payload.e2ee if isinstance(payload.e2ee, dict) else None
    encrypted_rich = envelope is not None and "rich_payload_digest" in envelope
    if not encrypted_rich:
        return False, [], []
    if envelope is None:
        raise RuntimeError("encrypted rich message lost its envelope")
    raw_stickers = envelope.get("message_sticker_refs", [])
    raw_emojis = envelope.get("message_custom_emoji_refs", [])
    if not isinstance(raw_stickers, list) or not isinstance(raw_emojis, list):
        raise HTTPException(status_code=409, detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"})
    try:
        sticker_refs = [EntityRef(str(item)) for item in raw_stickers]
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
        ) from exc
    return True, sticker_refs, [str(item) for item in raw_emojis]


async def acquire_message_expression_authorizations(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    channel: Channel,
    payload: MessageCreate,
    actor_permissions: Permission,
    *,
    application_ref: tuple[int, str] | None,
    expression_tokens: list[str],
    sticker_refs: list[EntityRef],
    authorize: bool,
) -> tuple[dict[str, dict[str, object]], list[dict[str, object]]] | None:
    if not authorize or not (expression_tokens or sticker_refs or payload.expression_actor_intents):
        return None
    if access.guild is None:
        raise RuntimeError("guild expression authorization lost its target")
    operation_id = payload.client_nonce or secrets.token_urlsafe(24)
    authorizations, sticker_items = await acquire_expression_use_authorizations(
        session,
        redis,
        settings,
        actor,
        application_ref=application_ref,
        actor_intents=payload.expression_actor_intents,
        target_guild_ref=f"{access.guild.id}@{access.guild.origin_domain}",
        target_channel_ref=f"{channel.id}@{channel.origin_domain}",
        target_message_ref=None,
        operation="message.create",
        operation_id=operation_id,
        emoji_tokens=expression_tokens,
        sticker_refs=sticker_refs,
    )
    if access.guild.origin_domain == settings.domain:
        attested_tokens, attested_items = await validate_expression_authorization_map(
            session,
            redis,
            settings,
            authorizations,
            requester_ref=f"{actor.id}@{actor.origin_domain}",
            requester_type=cast(Literal["human", "bot"], actor.account_type),
            application_ref=qualified_pair(application_ref),
            target_guild_ref=f"{access.guild.id}@{access.guild.origin_domain}",
            target_channel_ref=f"{channel.id}@{channel.origin_domain}",
            target_message_ref=None,
            operation="message.create",
            operation_id=operation_id,
            emoji_tokens=expression_tokens,
            sticker_items=sticker_items,
        )
        await validate_attested_expression_target(
            session,
            actor,
            access.guild,
            int(actor_permissions),
            attested_tokens,
            attested_items,
        )
    return authorizations, sticker_items


async def resolve_local_message_expressions(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    payload: MessageCreate,
    options: MessageAdmissionOptions,
    actor_permissions: Permission,
    *,
    encrypted_rich: bool,
    encrypted_custom_emoji_tokens: list[str],
    sticker_refs: list[EntityRef],
    poll_result_source: Message | None,
) -> list[dict[str, object]]:
    if payload.expression_actor_intents:
        raise ValueError("expression actor intents do not match a guild expression")
    if options.webhook_id is None and poll_result_source is None:
        await validate_custom_emoji_use(
            session,
            actor,
            payload.content,
            target_guild=access.guild,
            target_permissions=actor_permissions,
        )
        await validate_custom_sticker_use(
            session,
            actor,
            payload.content,
            target_guild=access.guild,
            target_permissions=actor_permissions,
        )
    await resolve_rich_custom_emojis(
        session,
        actor,
        components=payload.components,
        poll=payload.poll,
        default_domain=settings.domain,
        target_guild=access.guild,
        target_permissions=actor_permissions,
    )
    if encrypted_rich:
        await validate_custom_emoji_tokens(
            session,
            actor,
            encrypted_custom_emoji_tokens,
            target_guild=access.guild,
            target_permissions=actor_permissions,
        )
    return await resolve_sticker_items(
        session,
        actor,
        sticker_refs,
        default_domain=(
            access.guild.origin_domain if access.guild is not None else settings.domain
        ),
        target_guild=access.guild,
        target_permissions=actor_permissions,
        maximum=9 if encrypted_rich else 3,
    )


async def prepare_message_create_expressions(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    channel: Channel,
    payload: MessageCreate,
    options: MessageAdmissionOptions,
    actor_permissions: Permission,
    poll_result_source: Message | None,
) -> MessageCreateExpressions:
    encrypted_rich, encrypted_sticker_refs, encrypted_emoji_tokens = (
        encrypted_message_expression_refs(payload)
    )
    application_ref = (
        (options.application_id, options.application_domain)
        if options.application_id is not None and options.application_domain is not None
        else None
    )
    sticker_refs = encrypted_sticker_refs if encrypted_rich else payload.sticker_ids
    authorize = (
        access.guild is not None
        and options.webhook_id is None
        and poll_result_source is None
        # A plaintext forward may include an independently-authored note; its
        # expressions need fresh S authorization. Encrypted forward routing is
        # the preserved source snapshot and remains covered by its forward proof.
        and (payload.forwarded_message_id is None or not encrypted_rich)
    )
    try:
        expression_tokens = (
            expression_custom_emoji_tokens(
                content=payload.content,
                components=payload.components,
                poll=payload.poll,
                e2ee=payload.e2ee,
                default_domain=(
                    access.guild.origin_domain if access.guild is not None else settings.domain
                ),
            )
            if authorize
            else []
        )
        authorized = await acquire_message_expression_authorizations(
            session,
            redis,
            settings,
            access,
            actor,
            channel,
            payload,
            actor_permissions,
            application_ref=application_ref,
            expression_tokens=expression_tokens,
            sticker_refs=sticker_refs,
            authorize=authorize,
        )
        if authorized is None:
            authorizations: dict[str, dict[str, object]] = {}
            sticker_items = await resolve_local_message_expressions(
                session,
                settings,
                access,
                actor,
                payload,
                options,
                actor_permissions,
                encrypted_rich=encrypted_rich,
                encrypted_custom_emoji_tokens=encrypted_emoji_tokens,
                sticker_refs=sticker_refs,
                poll_result_source=poll_result_source,
            )
        else:
            authorizations, sticker_items = authorized
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
        ) from exc
    return MessageCreateExpressions(
        encrypted_rich=encrypted_rich,
        encrypted_custom_emoji_tokens=encrypted_emoji_tokens,
        application_ref=application_ref,
        authorizations=authorizations,
        sticker_items=sticker_items,
    )


@dataclass(frozen=True, slots=True)
class FederatedMessageCreateProjection:
    """Prepared local state projected into a remote guild create proposal."""

    requested_tts: bool
    sticker_items: list[dict[str, object]]
    expression_authorizations: dict[str, dict[str, object]]
    forwarded_ref: tuple[int, str] | None
    forwarded_channel_ref: tuple[int, str] | None
    forward_snapshot: dict[str, object] | None
    forward_source_nsfw: bool | None
    forward_source_proof: dict[str, object] | None
    application_ref: tuple[int, str] | None
    message_view_lineage: tuple[str, int, str, int] | None
    effective_view_timeout: int | None
    effective_view_persistent: bool
    referenced: Message | None
    explicit_mention_pairs: list[tuple[int, str]]
    message_attachments: list[Attachment]
    encrypted_poll: dict[str, object] | None
    encrypted_rich: bool
    has_message_view: bool


def qualified_pair(ref: tuple[int, str] | None) -> str | None:
    return f"{ref[0]}@{ref[1]}" if ref is not None else None


def federated_message_create_payload(
    actor: User,
    channel: Channel,
    payload: MessageCreate,
    options: MessageAdmissionOptions,
    projection: FederatedMessageCreateProjection,
) -> dict[str, object]:
    """Build the one canonical proposal used for live and queued delivery."""

    lineage = projection.message_view_lineage
    result: dict[str, object] = {
        "operation": "message.create",
        "actor": profile_from_user(actor),
        "channel_id": str(channel.id),
        "content": payload.content,
        "e2ee": payload.e2ee,
        "tts": projection.requested_tts,
        "flags": payload.flags,
        "embeds": [item.model_dump(mode="json", exclude_none=True) for item in payload.embeds],
        "components": [
            item.model_dump(mode="json", exclude_none=True) for item in payload.components
        ],
        "poll": (
            payload.poll.model_dump(mode="json", exclude_none=True)
            if payload.poll is not None
            else None
        ),
        "sticker_items": projection.sticker_items,
        "expression_authorizations": projection.expression_authorizations,
        "forwarded_message_id": qualified_pair(projection.forwarded_ref),
        "forwarded_channel_id": qualified_pair(projection.forwarded_channel_ref),
        "forward_snapshot": projection.forward_snapshot,
        "forward_source_nsfw": projection.forward_source_nsfw,
        "forward_source_proof": projection.forward_source_proof,
        "application_id": qualified_pair(projection.application_ref),
        "interaction_integration_type": lineage[0] if lineage is not None else None,
        "interaction_installation_ref": (
            qualified_pair((lineage[1], lineage[2])) if lineage is not None else None
        ),
        "interaction_installation_revision": str(lineage[3]) if lineage is not None else None,
        "interaction_message_type": options.interaction_message_type,
        "interaction_metadata": options.interaction_metadata,
        "view_timeout_seconds": projection.effective_view_timeout,
        "view_persistent": projection.effective_view_persistent,
        "client_nonce": payload.client_nonce,
        "referenced_message_id": (
            qualified_pair((projection.referenced.id, projection.referenced.origin_domain))
            if projection.referenced is not None
            else None
        ),
        "mention_user_ids": [
            f"{user_id}@{domain}" for user_id, domain in projection.explicit_mention_pairs
        ],
        "attachments": [
            federation_attachment_payload(item) for item in projection.message_attachments
        ],
    }
    if payload.voice_message:
        result["voice_message"] = True
    if "allowed_mentions" in payload.model_fields_set:
        result["allowed_mentions"] = (
            payload.allowed_mentions.model_dump(mode="json")
            if payload.allowed_mentions is not None
            else None
        )
    return result


async def proxy_remote_guild_message_create(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    response_status: Response,
    access: ChannelAccess,
    actor: User,
    channel: Channel,
    payload: MessageCreate,
    options: MessageAdmissionOptions,
    projection: FederatedMessageCreateProjection,
) -> dict[str, object]:
    """Submit one prepared create to its guild authority and bind the replica."""

    guild = access.guild
    if guild is None or guild.origin_domain == settings.domain:
        raise RuntimeError("remote guild message proxy requires a remote guild")
    if payload.client_nonce is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "CLIENT_NONCE_REQUIRED_FOR_FEDERATION"},
        )

    message_attachments = projection.message_attachments
    proxy_payload = federated_message_create_payload(
        actor,
        channel,
        payload,
        options,
        projection,
    )
    if message_attachments:
        # The remote guild home can commit this proposal and fan its
        # attachment metadata out before our HTTP response is observed.
        # Durably remember that authority before making the request so a
        # crash cannot strand a later terminal media tombstone.
        await record_attachment_recipients(
            session,
            {(item.id, item.origin_domain) for item in message_attachments},
            guild.origin_domain,
            room_ref=("guild", guild.id, guild.origin_domain),
        )
        await session.commit()
    replica_was_quota_paused = guild.sync_status == "quota_paused"
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/proxy",
            payload=proxy_payload,
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError) as exc:
        # S receipts are deliberately short-lived and cannot safely sit in
        # the durable guild proxy queue. A retry reacquires current source
        # membership/role/availability proof under the same client nonce.
        if projection.expression_authorizations:
            raise HTTPException(
                status_code=503,
                detail={"code": "EXPRESSION_AUTHORIZATION_UNAVAILABLE"},
            ) from exc
        envelope = await build_envelope(
            session,
            settings,
            "guild.proxy.message.create",
            actor,
            proxy_payload,
            context={
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
            },
        )
        await queue_event(session, settings, guild.origin_domain, envelope)
        await session.commit()
        await enqueue_best_effort(federation_deliver, guild.origin_domain)
        for attachment in message_attachments:
            await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        response_status.status_code = status.HTTP_202_ACCEPTED
        return {"status": "queued", "client_nonce": payload.client_nonce}
    raise_proxy_rejection(response, {400, 403, 404, 409, 429, 507})
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"})
    try:
        proxied = decode_federation_response_json(response)
    except FederationNetworkError:
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
        ) from None

    response_validation_stage = "response_shape"
    try:
        if (
            not isinstance(proxied, dict)
            or set(proxied) != {"message", "event", "seq"}
            or not isinstance(proxied.get("message"), dict)
            or not isinstance(proxied.get("event"), dict)
        ):
            raise ValueError("guild home returned an invalid proxy response")
        response_validation_stage = "event_signature"
        committed_envelope = await validated_event_envelope(
            session,
            settings,
            guild.origin_domain,
            proxied["event"],
        )
        event = committed_envelope.model_dump(mode="json")
        response_validation_stage = "event_projection"
        context = event.get("context")
        content = event.get("content")
        event_message = content.get("message") if isinstance(content, dict) else None
        expected_attachment_payloads = [attachment_payload(item) for item in message_attachments]
        expected_remote_attachment_payloads = []
        for expected_attachment in expected_attachment_payloads:
            projected_attachment = dict(expected_attachment)
            projected_attachment["scan_status"] = (
                "encrypted" if projected_attachment.get("encryption_mode") == "e2ee" else "clean"
            )
            expected_remote_attachment_payloads.append(projected_attachment)
        expected_remote_attachment_payloads.sort(
            key=lambda item: (
                str(item.get("origin_domain", "")),
                int(str(item.get("id", "0"))),
            )
        )
        expected_attachment_refs = {(item.id, item.origin_domain) for item in message_attachments}
        expected_embeds = [
            item.model_dump(mode="json", exclude_none=True) for item in payload.embeds
        ]
        expected_components = [
            item.model_dump(mode="json", exclude_none=True) for item in payload.components
        ]
        event_poll = event_message.get("poll") if isinstance(event_message, dict) else None
        event_created_at = (
            event_message.get("created_at") if isinstance(event_message, dict) else None
        )
        expected_poll_answers = (
            [
                {
                    "answer_id": answer_id,
                    "poll_media": answer.poll_media.model_dump(mode="json", exclude_none=True),
                }
                for answer_id, answer in enumerate(payload.poll.answers, start=1)
            ]
            if payload.poll is not None
            else None
        )
        encrypted_poll = projection.encrypted_poll
        poll_matches = event_poll is None and payload.poll is None and encrypted_poll is None
        if payload.poll is not None and isinstance(event_poll, dict):
            try:
                poll_expiry = datetime.fromisoformat(str(event_poll.get("expiry")))
                message_created = datetime.fromisoformat(str(event_created_at))
            except (TypeError, ValueError):
                poll_matches = False
            else:
                poll_matches = bool(
                    event_poll.get("question")
                    == payload.poll.question.model_dump(mode="json", exclude_none=True)
                    and event_poll.get("answers") == expected_poll_answers
                    and event_poll.get("allow_multiselect") == payload.poll.allow_multiselect
                    and event_poll.get("layout_type") == payload.poll.layout_type
                    and abs(
                        (
                            poll_expiry - message_created - timedelta(hours=payload.poll.duration)
                        ).total_seconds()
                    )
                    <= 2
                )
        elif encrypted_poll is not None and isinstance(event_poll, dict):
            try:
                poll_expiry = datetime.fromisoformat(str(event_poll.get("expiry")))
                message_created = datetime.fromisoformat(str(event_created_at))
            except (TypeError, ValueError):
                poll_matches = False
            else:
                answer_ids = encrypted_poll["answer_ids"]
                poll_matches = bool(
                    event_poll.get("encrypted") is True
                    and event_poll.get("answer_ids") == answer_ids
                    and event_poll.get("allow_multiselect") is encrypted_poll["allow_multiselect"]
                    and event_poll.get("layout_type") == 1
                    and abs(
                        (
                            poll_expiry
                            - message_created
                            - timedelta(seconds=cast(int, encrypted_poll["duration_seconds"]))
                        ).total_seconds()
                    )
                    <= 2
                )
        application_ref = projection.application_ref
        forwarded_ref = projection.forwarded_ref
        referenced = projection.referenced
        if (
            event.get("type") != "guild.message.committed"
            or not isinstance(context, dict)
            or not isinstance(event_message, dict)
            or context.get("guild_id") != str(guild.id)
            or context.get("guild_domain") != guild.origin_domain
            or context.get("seq") != proxied.get("seq")
            or event_message != proxied["message"]
            or event_message.get("origin_domain") != guild.origin_domain
            or event_message.get("channel_id") != str(channel.id)
            or event_message.get("channel_domain") != channel.origin_domain
            or event_message.get("author_id") != str(actor.id)
            or event_message.get("author_domain") != actor.origin_domain
            or event_message.get("content") != payload.content
            or event_message.get("e2ee") != payload.e2ee
            or event_message.get("tts", False) is not projection.requested_tts
            or bool(int(event_message.get("flags", 0)) & MESSAGE_FLAG_IS_VOICE_MESSAGE)
            != payload.voice_message
            or (
                int(event_message.get("flags", 0))
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
                    components_v2=(
                        uses_components_v2(payload.components)
                        or bool(
                            projection.encrypted_rich
                            and payload.flags & MESSAGE_FLAG_IS_COMPONENTS_V2
                        )
                    ),
                )
            )
            or event_message.get("embeds") != expected_embeds
            or event_message.get("components") != expected_components
            or event_message.get("application_id")
            != (str(application_ref[0]) if application_ref is not None else None)
            or event_message.get("application_domain")
            != (application_ref[1] if application_ref is not None else None)
            or event_message.get("view_version")
            != (1 if projection.has_message_view and application_ref is not None else 0)
            or event_message.get("forwarded_message_id")
            != (str(forwarded_ref[0]) if forwarded_ref is not None else None)
            or event_message.get("forwarded_message_domain")
            != (forwarded_ref[1] if forwarded_ref is not None else None)
            or not poll_matches
            or event_message.get("client_nonce") != payload.client_nonce
            # The authority is allowed to project the lifecycle status it
            # assigns while validating the proxy request (plaintext
            # references become ``clean`` and E2EE references become
            # ``encrypted``). Every immutable metadata field and the exact
            # ordered reference set must still match the request.
            or event_message.get("attachments") != expected_remote_attachment_payloads
            or message_attachment_refs(event) != expected_attachment_refs
            or event_message.get("referenced_message_id")
            != (str(referenced.id) if referenced is not None else None)
            or event_message.get("referenced_message_domain")
            != (referenced.origin_domain if referenced is not None else None)
        ):
            raise ValueError("guild home returned a mismatched proxy event")

        # The request crossed a transaction boundary while the remote home
        # committed the proposal. Re-enter the room/media fence and reload the
        # projection before applying or binding anything: an exact terminal
        # guild control may have won while HTTP was in flight.
        response_validation_stage = "replica_fence"
        await lock_terminal_room(session, "guild", guild.id, guild.origin_domain)
        for attachment_id, attachment_domain in sorted(
            expected_attachment_refs,
            key=lambda item: (item[1], item[0]),
        ):
            await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
        terminal_receipt = await session.get(
            TerminalRoomDeletion,
            ("guild", guild.id, guild.origin_domain, settings.domain),
        )
        live_guild = await session.get(
            Guild,
            (guild.id, guild.origin_domain),
            populate_existing=True,
        )
        live_channel = await session.get(
            Channel,
            (channel.id, channel.origin_domain),
            populate_existing=True,
        )
        if (
            terminal_receipt is not None
            or live_guild is None
            or live_guild.unavailable
            or live_channel is None
            or live_channel.unavailable
        ):
            raise HTTPException(status_code=410, detail={"code": "GUILD_DELETED"})
        for item in message_attachments:
            if (
                await session.get(
                    MediaTombstoneSource,
                    (item.id, item.origin_domain),
                )
                is not None
            ):
                raise HTTPException(status_code=410, detail={"code": "ATTACHMENT_DELETED"})
        access = ChannelAccess(channel=live_channel, guild=live_guild, participants=[])
        channel = live_channel
        response_validation_stage = "replica_apply"
        try:
            replicated = await apply_guild_message_event(session, settings, live_guild, event)
        except GuildSequenceGap:
            live_guild.sync_status = "stale"
            await session.commit()
            await enqueue_best_effort(
                federation_guild_sync,
                live_guild.origin_domain,
                live_guild.id,
            )
            for attachment in message_attachments:
                await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
            response_status.status_code = status.HTTP_202_ACCEPTED
            return {"status": "queued", "client_nonce": payload.client_nonce}
        try:
            await admit_replica_storage(session, settings, live_guild)
        except FederationReplicaQuotaExceeded as exc:
            guild_id = live_guild.id
            guild_domain = live_guild.origin_domain
            await session.rollback()
            await mark_replica_quota_paused(
                session,
                settings,
                guild_id,
                guild_domain,
                exc,
            )
            await session.commit()
            quota_guild = await session.get(
                Guild,
                (guild_id, guild_domain),
                populate_existing=True,
            )
            if quota_guild is not None:
                await publish_replica_guild_status(redis, quota_guild)
            raise HTTPException(
                status_code=507,
                detail={"code": REPLICA_QUOTA_ERROR_CODE},
            ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        log.exception(
            "federated_message_proxy_response_invalid",
            guild_id=str(guild.id),
            guild_domain=guild.origin_domain,
            channel_id=str(channel.id),
            channel_domain=channel.origin_domain,
            validation_stage=response_validation_stage,
            reason=str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
        ) from None
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_WRITE_UNAVAILABLE"},
        ) from None
    if replicated is None:
        replicated = await session.get(
            Message,
            (int(event_message["id"]), event_message["origin_domain"]),
        )
    if replicated is None:
        raise RuntimeError("authoritative guild message was not replicated")
    for attachment in message_attachments:
        refreshed_attachment = await session.get(
            Attachment,
            (attachment.id, attachment.origin_domain),
            populate_existing=True,
        )
        if (
            refreshed_attachment is None
            or refreshed_attachment.deleted_at is not None
            or await session.get(
                MediaTombstoneSource,
                (attachment.id, attachment.origin_domain),
            )
            is not None
        ):
            raise HTTPException(status_code=410, detail={"code": "ATTACHMENT_DELETED"})
        refreshed_attachment.message_id = replicated.id
        refreshed_attachment.message_domain = replicated.origin_domain
    await session.commit()
    if replica_was_quota_paused:
        refreshed_guild = await session.get(
            Guild,
            (live_guild.id, live_guild.origin_domain),
            populate_existing=True,
        )
        if refreshed_guild is not None:
            await publish_replica_guild_status(redis, refreshed_guild)
    for attachment in message_attachments:
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
    result = await render_message_payload(session, replicated, actor, viewer=actor)
    await publish_channel_dispatch(redis, access, "MESSAGE_CREATE", result)
    return result


def remote_allowed_message_mentions(
    payload: MessageCreate,
    settings: Settings,
    access: ChannelAccess,
    referenced: Message | None,
) -> MessageCreateMentions:
    """Preserve syntactic intent for expansion by a remote guild authority."""

    if access.guild is None:
        raise RuntimeError("federated guild mention projection lost its guild")
    mention_policy = regular_message_allowed_mentions(payload.allowed_mentions)
    selection = selected_allowed_mentions(
        mention_policy,
        allowed_mention_texts(payload.content, payload.components),
        settings,
        default_domain=access.guild.origin_domain,
    )
    explicit_recipients = sorted(selection.users)
    if mention_policy.replied_user and referenced is not None:
        explicit_recipients = sorted(
            {*explicit_recipients, (referenced.author_id, referenced.author_domain)}
        )
    return MessageCreateMentions(
        explicit_recipients=explicit_recipients,
        recipients=explicit_recipients,
        role_recipients=set(),
        roles=sorted(selection.roles),
        everyone=selection.everyone,
    )


async def legacy_message_mentions(
    session: AsyncSession,
    access: ChannelAccess,
    settings: Settings,
    payload: MessageCreate,
    actor_permissions: Permission,
) -> MessageCreateMentions:
    """Resolve the pre-allowed-mentions request shape through one compatibility path."""

    explicit_recipients = list(
        dict.fromkeys(item.resolve(settings.domain) for item in payload.mention_user_ids)
    )
    recipients = explicit_recipients
    role_recipients: set[tuple[int, str]] = set()
    roles: list[tuple[int, str]] = []
    everyone = False
    if access.guild is not None:
        visible_text = message_automod_text(payload.content, components=payload.components)
        roles = role_mention_refs(visible_text)
        role_recipients = set(
            await role_mention_recipients(
                session,
                access.guild,
                visible_text,
                actor_permissions,
            )
        )
        recipients = merge_mention_recipients(recipients, list(role_recipients))
        everyone = bool(isinstance(visible_text, str) and EVERYONE_MENTION.search(visible_text))
        if everyone:
            recipients = merge_mention_recipients(
                recipients,
                list(
                    await everyone_mention_recipients(
                        session,
                        access,
                        actor_permissions,
                    )
                ),
            )
    return MessageCreateMentions(
        explicit_recipients=explicit_recipients,
        recipients=recipients,
        role_recipients=role_recipients,
        roles=roles,
        everyone=everyone,
    )


async def resolve_message_create_mentions(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    auth: AuthenticatedUser,
    payload: MessageCreate,
    options: MessageAdmissionOptions,
    *,
    actor_permissions: Permission,
    application_ref: tuple[int, str] | None,
    referenced: Message | None,
    poll_result_source: Message | None,
) -> MessageCreateMentions:
    """Resolve every admission shape into one canonical mention projection."""

    encrypted = await resolve_encrypted_rich_mention_projection(
        session,
        access,
        payload.e2ee,
        actor_permissions=actor_permissions,
        referenced=referenced,
    )
    if encrypted is not None:
        encrypted_recipients = list(encrypted.recipients)
        if (
            options.authoritative_mention_refs is not None
            and tuple(encrypted_recipients) != options.authoritative_mention_refs
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
            )
        projection = MessageCreateMentions(
            explicit_recipients=encrypted_recipients,
            recipients=encrypted_recipients,
            role_recipients=set(encrypted.role_recipients),
            roles=list(encrypted.roles),
            everyone=encrypted.everyone,
        )
    elif options.authoritative_mention_refs is not None:
        projection = authoritative_message_mentions(options)
    elif "allowed_mentions" in payload.model_fields_set or (
        application_ref is not None
        and options.webhook_id is None
        and options.interaction_permissions is None
    ):
        if access.guild is not None and access.guild.origin_domain != settings.domain:
            projection = remote_allowed_message_mentions(payload, settings, access, referenced)
        else:
            resolved = await resolve_allowed_mentions_projection(
                session,
                redis,
                settings,
                access,
                auth.user,
                regular_message_allowed_mentions(payload.allowed_mentions),
                payload.content,
                payload.components,
                actor_permissions=actor_permissions,
                replied_user_ref=(
                    (referenced.author_id, referenced.author_domain)
                    if referenced is not None
                    else None
                ),
            )
            projection = MessageCreateMentions(
                explicit_recipients=list(resolved.user_recipients),
                recipients=list(resolved.recipients),
                role_recipients=set(resolved.role_recipients),
                roles=list(resolved.roles),
                everyone=resolved.everyone,
            )
    else:
        projection = await legacy_message_mentions(
            session,
            access,
            settings,
            payload,
            actor_permissions,
        )
    if poll_result_source is None:
        await require_valid_message_mentions(session, access, projection.recipients)
    return projection


def authoritative_message_mentions(options: MessageAdmissionOptions) -> MessageCreateMentions:
    """Restore an already-authorized application/webhook mention projection."""

    if options.authoritative_mention_refs is None:
        raise ValueError("authoritative mention recipients are required")
    recipients = list(dict.fromkeys(options.authoritative_mention_refs))
    return MessageCreateMentions(
        explicit_recipients=recipients,
        recipients=recipients,
        role_recipients=set(options.authoritative_mention_role_recipient_refs or ()),
        roles=list(dict.fromkeys(options.authoritative_mention_role_refs or ())),
        everyone=bool(options.authoritative_mention_everyone),
    )


@dataclass(frozen=True, slots=True)
class MessageCreateAttachments:
    replicated: list[dict[str, object]]
    local: list[Attachment]


async def prepare_message_create_attachments(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    channel: Channel,
    payload: MessageCreate,
    options: MessageAdmissionOptions,
) -> MessageCreateAttachments:
    """Validate and finalize the exact attachment set before message admission."""

    replicated = list(options.replicated_attachments)
    local: list[Attachment] = []
    if replicated:
        try:
            replicated_ids = [int(str(item["id"])) for item in replicated if isinstance(item, dict)]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail={"code": "FEDERATED_ATTACHMENT_INVALID"},
            ) from None
        if len(replicated_ids) != len(replicated) or replicated_ids != [
            int(item) for item in payload.attachment_ids
        ]:
            raise HTTPException(
                status_code=400,
                detail={"code": "FEDERATED_ATTACHMENT_INVALID"},
            )
    else:
        for attachment_id in payload.attachment_ids:
            attachment = await finalize_attachment(
                session,
                settings,
                actor,
                int(attachment_id),
                required_purpose=options.required_attachment_purpose,
                federated_guild_upload=options.federated_guild_upload,
            )
            require_attachment_upload_channel(attachment, channel)
            if (
                attachment.message_id is not None
                or attachment.message_domain is not None
                or attachment.interaction_id is not None
                or attachment.interaction_response_id is not None
                or getattr(attachment, "report_id", None) is not None
            ):
                raise HTTPException(status_code=409, detail={"code": "ATTACHMENT_ALREADY_USED"})
            if options.required_attachment_binding_prefix is not None:
                expected_binding = f"{options.required_attachment_binding_prefix}{attachment.id}"
                if attachment.asset_binding != expected_binding:
                    raise HTTPException(
                        status_code=404,
                        detail={"code": "ATTACHMENT_NOT_FOUND"},
                    )
                attachment.asset_binding = None
            local.append(attachment)
    attachments: Sequence[Attachment | dict[str, object]] = replicated or local
    require_voice_message_attachments(payload.voice_message, attachments)
    try:
        validate_attachment_url_references(
            embeds=payload.embeds,
            components=payload.components,
            attachments=attachments,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "ATTACHMENT_REFERENCE_INVALID"},
        ) from exc
    return MessageCreateAttachments(replicated=replicated, local=local)


async def enforce_message_create_slowmode(
    redis: Redis,
    access: ChannelAccess,
    channel: Channel,
    actor: User,
    actor_permissions: Permission,
) -> None:
    if (
        access.guild is None
        or not channel.rate_limit_per_user
        or actor.account_type == "bot"
        or actor_permissions & Permission.BYPASS_SLOWMODE
    ):
        return
    slowmode_key = f"slowmode:{channel.origin_domain}:{channel.id}:{actor.origin_domain}:{actor.id}"
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
                "retry_after_ms": await slowmode_retry_after_ms(redis, slowmode_key),
            },
        )


@router.post("/{channel_id}/messages")
async def create_message(
    channel_id: EntityRef,
    payload: MessageCreate,
    response_status: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    admission_options: MessageAdmissionOptions = Depends(default_message_admission_options),
) -> dict[str, object]:
    if not isinstance(admission_options, MessageAdmissionOptions):
        # Direct service callers omit FastAPI-resolved dependency defaults.
        admission_options = default_message_admission_options()
    legacy_stickers = custom_sticker_refs(payload.content)
    if (
        not payload.sticker_ids
        and len(legacy_stickers) == 1
        and payload.content is not None
        and payload.content.strip() == legacy_stickers[0].token
    ):
        legacy = legacy_stickers[0]
        payload.sticker_ids = [EntityRef(f"{legacy.id}@{legacy.origin_domain}")]
        payload.content = None
    requested_tts = payload.tts or admission_options.tts
    if not admission_options.skip_client_rate_limit and admission_options.poll_result is None:
        await enforce_client_rate_limit(
            redis,
            response_status,
            CLIENT_RATE_LIMITS["message_send"],
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
        )
    access = await load_message_create_access(
        session,
        settings,
        auth,
        channel_id,
        admission_options,
    )
    if admission_options.transaction is not None and (
        access.guild is None or access.guild.origin_domain != settings.domain
    ):
        raise RuntimeError("Transaction-owned message creation requires the local guild authority.")
    access, prelock_conversation = await lock_message_create_access(
        session,
        settings,
        access,
        payload.attachment_ids,
    )
    channel = access.channel
    poll_result_source = (
        await validate_poll_result_admission(
            session,
            channel,
            auth.user,
            payload,
            admission_options.poll_result,
        )
        if admission_options.poll_result is not None
        else None
    )
    pending_starter = (
        await session.get(
            EncryptedForumStarterReservation,
            (channel.id, channel.origin_domain),
        )
        if channel.type in THREAD_CHANNEL_TYPES
        else None
    )
    if (
        pending_starter is not None
        and pending_starter.claimed_at is None
        and not admission_options.mark_thread_starter
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_FORUM_STARTER_CLAIM_REQUIRED"},
        )
    prior_thread_message_projection = (
        capture_thread_message_projection(channel) if channel.type in THREAD_CHANNEL_TYPES else None
    )
    if poll_result_source is not None:
        actor_permissions = Permission(ALL_PERMISSIONS)
    elif admission_options.webhook_id is not None:
        actor_permissions = Permission(WEBHOOK_CAPABILITY_MESSAGE_PERMISSIONS)
    else:
        needed = message_create_permissions(
            payload,
            guild_channel=access.guild is not None,
            forum_starter_permissions_checked=(admission_options.forum_starter_permissions_checked),
        )
        if admission_options.interaction_permissions is not None and access.guild is not None:
            needed = channel_message_permissions(channel, needed)
            actor_permissions = Permission(admission_options.interaction_permissions)
            if actor_permissions & needed != needed:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "MISSING_PERMISSIONS",
                        "message": (
                            "The interaction permission snapshot does not allow this response."
                        ),
                        "permissions": str(int(needed)),
                    },
                )
        else:
            actor_permissions = Permission(
                await require_channel_permissions(
                    session,
                    redis,
                    access,
                    auth.user,
                    needed,
                )
            )
    if access.guild is not None and access.guild.origin_domain == settings.domain:
        await require_voice_message_guild_capacity(
            session,
            access.guild,
            voice_message=payload.voice_message,
        )
    if (
        channel.type in THREAD_CHANNEL_TYPES
        and bool(channel.locked)
        and not actor_permissions & Permission.MANAGE_THREADS
    ):
        raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})
    expressions = await prepare_message_create_expressions(
        session,
        redis,
        settings,
        access,
        auth.user,
        channel,
        payload,
        admission_options,
        actor_permissions,
        poll_result_source,
    )
    encrypted_rich = expressions.encrypted_rich
    encrypted_custom_emoji_tokens = expressions.encrypted_custom_emoji_tokens
    application_ref = expressions.application_ref
    expression_authorizations = expressions.authorizations
    sticker_items = expressions.sticker_items
    if admission_options.interaction_permissions is None and poll_result_source is None:
        await require_dm_send(session, access, auth.user)
    if not is_message_capable_channel_type(
        channel.type,
        guild_channel=access.guild is not None,
    ):
        raise HTTPException(status_code=400, detail={"code": "NOT_TEXT_CHANNEL"})
    encrypted_contract, encrypted_controls, encrypted_poll = encrypted_rich_routing(payload.e2ee)
    has_message_view = bool(payload.components or encrypted_controls)
    effective_view_persistent = (
        bool(payload.e2ee.get("view_persistent"))
        if encrypted_rich and isinstance(payload.e2ee, dict)
        else payload.view_persistent
    )
    encrypted_view_timeout = (
        encrypted_contract.get("view_timeout_seconds") if encrypted_contract is not None else None
    )
    effective_view_timeout = (
        int(encrypted_view_timeout)
        if isinstance(encrypted_view_timeout, int) and not isinstance(encrypted_view_timeout, bool)
        else payload.view_timeout_seconds
    )
    message_view_expires_at = (
        datetime.now(UTC) + timedelta(seconds=effective_view_timeout or 900)
        if has_message_view and application_ref is not None and not effective_view_persistent
        else None
    )
    message_view_lineage = (
        await message_view_installation_lineage(
            session,
            settings,
            admission_options,
            federated_transport=(
                access.guild is not None and access.guild.origin_domain != settings.domain
            ),
        )
        if (has_message_view or encrypted_rich) and application_ref is not None
        else None
    )
    if (
        has_message_view
        and application_ref is None
        and not admission_options.allow_render_only_components
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "COMPONENT_APPLICATION_REQUIRED"},
        )
    forwarded_ref: tuple[int, str] | None = None
    forwarded_channel_ref: tuple[int, str] | None = None
    forward_snapshot: dict[str, object] | None = None
    forward_source_nsfw: bool | None = None
    forward_source_projection_digest: str | None = None
    forward_source_sticker_items: list[dict[str, object]] = []
    forward_source_custom_emoji_refs: list[str] = []
    forwarded_created_at: datetime | None = None
    forwarded_edited_at: datetime | None = None
    forwarded_flags: int | None = None
    forwarded_message_type: int | None = None
    source_attachments: list[Attachment] = []
    source_attachment_count = 0
    forward_source_proof: dict[str, object] | None = None
    encrypted_forward = encrypted_rich and payload.forwarded_message_id is not None
    encrypted_forward_source = False
    if payload.forwarded_message_id is not None:
        if payload.client_nonce is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "FORWARD_CLIENT_NONCE_REQUIRED"},
            )
        forwarded_ref = payload.forwarded_message_id.resolve(settings.domain)
        source_message = await session.get(Message, forwarded_ref)
        raw_proof_content = (
            payload.forward_source_proof.get("content")
            if isinstance(payload.forward_source_proof, dict)
            else None
        )
        raw_proof_channel_ref = (
            raw_proof_content.get("source_channel_ref")
            if isinstance(raw_proof_content, dict)
            else None
        )
        try:
            proof_channel_ref = (
                EntityRef(str(raw_proof_channel_ref)).resolve(settings.domain)
                if raw_proof_channel_ref is not None
                else None
            )
        except ValueError:
            proof_channel_ref = None
        if source_message is not None and source_message.deleted_at is not None:
            raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
        source_channel_ref = (
            (source_message.channel_id, source_message.channel_domain)
            if source_message is not None
            else proof_channel_ref
        )
        if source_channel_ref is None:
            raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
        source_channel = await session.get(Channel, source_channel_ref)
        if source_channel is None or source_channel.unavailable:
            raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
        proof_device_id = admission_options.forward_source_e2ee_device_id
        if proof_device_id is None and auth.user.account_type == "bot":
            raw_device_id = payload.e2ee.get("sender_device_id") if payload.e2ee else None
            proof_device_id = raw_device_id if isinstance(raw_device_id, str) else None
        if payload.forward_source_proof is not None:
            forward_source_proof = dict(payload.forward_source_proof)
        elif source_channel.origin_domain == settings.domain:
            if source_message is None:
                raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
            source_access = await load_channel_access(
                session,
                settings,
                auth.user,
                EntityRef(f"{source_channel.id}@{source_channel.origin_domain}"),
            )
            await require_channel_permissions(
                session,
                redis,
                source_access,
                auth.user,
                required_permissions("message.list"),
            )
            source_poll = await session.get(
                Poll,
                (source_message.id, source_message.origin_domain),
            )
            if (
                source_poll is not None
                or source_message.message_type not in FORWARDABLE_MESSAGE_TYPES
            ):
                raise HTTPException(status_code=400, detail={"code": "MESSAGE_NOT_FORWARDABLE"})
            source_attachments = list(
                await session.scalars(
                    select(Attachment)
                    .where(
                        Attachment.message_id == source_message.id,
                        Attachment.message_domain == source_message.origin_domain,
                        Attachment.deleted_at.is_(None),
                    )
                    .order_by(Attachment.id, Attachment.origin_domain)
                )
            )
            local_source_nsfw = await effective_channel_nsfw(session, source_channel)
            if local_source_nsfw is None:
                raise _forward_proof_http_error("FORWARD_CONTEXT_UNSUPPORTED")
            forward_source_proof = await local_forward_source_proof(
                session,
                settings,
                requester=auth.user,
                source=source_message,
                source_channel=source_channel,
                destination_channel=channel,
                attachments=source_attachments,
                source_nsfw=local_source_nsfw,
                nonce=payload.client_nonce,
                application_ref=application_ref,
                e2ee_device_id=proof_device_id,
            )
        else:
            forward_source_proof = await remote_forward_source_proof(
                session,
                settings,
                requester=auth.user,
                source_message_ref=forwarded_ref,
                source_channel=source_channel,
                destination_channel=channel,
                nonce=payload.client_nonce,
            )
        proof = await validate_signed_forward_source_proof(
            session,
            settings,
            forward_source_proof,
            requester=auth.user,
            source_message_ref=forwarded_ref,
            source_channel_ref=source_channel_ref,
            destination_channel=channel,
            nonce=payload.client_nonce,
            application_ref=application_ref,
            e2ee_device_id=proof_device_id,
        )
        forward_source_nsfw = cast(bool, proof["source_nsfw"])
        if not can_forward_between_age_contexts(
            forward_source_nsfw,
            await effective_channel_nsfw(session, channel),
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "AGE_RESTRICTED_FORWARD_UNSUPPORTED"},
            )
        forwarded_channel_ref = EntityRef(cast(str, proof["source_channel_ref"])).resolve(
            settings.domain
        )
        forward_source_projection_digest = cast(
            str,
            proof["source_projection_digest"],
        )
        forward_source_sticker_items = cast(
            list[dict[str, object]],
            proof["source_sticker_items"],
        )
        forward_source_custom_emoji_refs = cast(
            list[str],
            proof["source_custom_emoji_refs"],
        )
        forwarded_created_at = datetime.fromisoformat(cast(str, proof["source_created_at"]))
        raw_edited_at = proof.get("source_edited_at")
        forwarded_edited_at = (
            datetime.fromisoformat(cast(str, raw_edited_at)) if raw_edited_at is not None else None
        )
        forwarded_flags = cast(int, proof["source_flags"])
        forwarded_message_type = cast(int, proof["source_message_type"])
        source_attachment_refs = cast(list[str], proof["source_attachment_refs"])
        source_attachment_count = len(source_attachment_refs)
        if (
            source_channel.origin_domain == settings.domain
            and sorted(f"{item.id}@{item.origin_domain}" for item in source_attachments)
            != source_attachment_refs
        ):
            raise _forward_proof_http_error()
        encrypted_forward_source = proof["source_encryption_mode"] == "e2ee"
        if encrypted_forward:
            resolved_stickers_by_ref = {
                f"{item['id']}@{item['origin_domain']}": item for item in sticker_items
            }
            if any(
                resolved_stickers_by_ref.get(f"{item['id']}@{item['origin_domain']}") != item
                for item in forward_source_sticker_items
            ) or not set(forward_source_custom_emoji_refs).issubset(encrypted_custom_emoji_tokens):
                raise _forward_proof_http_error()
        if encrypted_forward_source:
            if not encrypted_forward:
                if payload.forward_snapshot is None:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "E2EE_FORWARD_SNAPSHOT_REQUIRED"},
                    )
                try:
                    forward_snapshot = validate_forward_snapshot_source_binding(
                        payload.forward_snapshot,
                        source_projection_digest=forward_source_projection_digest,
                        source_created_at=forwarded_created_at,
                        source_edited_at=forwarded_edited_at,
                        source_flags=forwarded_flags,
                        source_message_type=forwarded_message_type,
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "E2EE_FORWARD_SNAPSHOT_MISMATCH"},
                    ) from exc
        else:
            authoritative_snapshot = proof.get("source_snapshot")
            if not isinstance(authoritative_snapshot, dict):
                raise _forward_proof_http_error()
            if encrypted_forward:
                forward_snapshot = None
            else:
                if payload.forward_snapshot is not None:
                    raise HTTPException(
                        status_code=400,
                        detail={"code": "FORWARD_SNAPSHOT_NOT_ALLOWED"},
                    )
                forward_snapshot = authoritative_snapshot
    if forward_snapshot is not None:
        try:
            forwarded_custom_emoji_tokens = forward_snapshot_custom_emoji_tokens(forward_snapshot)
            forwarded_sticker_items = forward_snapshot_sticker_items(forward_snapshot)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "FORWARD_SOURCE_PROOF_INVALID"},
            ) from exc
        if (
            forwarded_custom_emoji_tokens != forward_source_custom_emoji_refs
            or forwarded_sticker_items != forward_source_sticker_items
        ):
            raise _forward_proof_http_error()
        await validate_custom_emoji_tokens(
            session,
            auth.user,
            forwarded_custom_emoji_tokens,
            target_guild=access.guild,
            target_permissions=actor_permissions,
        )
        forwarded_sticker_refs = [
            EntityRef(f"{item['id']}@{item['origin_domain']}") for item in forwarded_sticker_items
        ]
        resolved_forwarded_stickers = await resolve_sticker_items(
            session,
            auth.user,
            forwarded_sticker_refs,
            default_domain=(
                access.guild.origin_domain if access.guild is not None else settings.domain
            ),
            target_guild=access.guild,
            target_permissions=actor_permissions,
            maximum=9,
        )
        if resolved_forwarded_stickers != forwarded_sticker_items:
            raise HTTPException(
                status_code=409,
                detail={"code": "FORWARD_SOURCE_PROOF_INVALID"},
            )
    thread_was_unarchived = False
    if channel.type in THREAD_CHANNEL_TYPES and bool(getattr(channel, "archived", False)):
        if bool(getattr(channel, "locked", False)) and not (
            actor_permissions & Permission.MANAGE_THREADS
        ):
            raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})
        if access.guild is None:
            raise RuntimeError("guild thread has no guild")
        await require_active_thread_capacity(
            session,
            access.guild,
            excluding=(channel.id, channel.origin_domain),
        )
        thread_was_unarchived = True
    dm_conversation = prelock_conversation if access.guild is None else None
    if dm_conversation is not None:
        # Serialize validation with rolling eviction so a reply target cannot
        # disappear between lookup and message admission.
        await lock_federated_dm_authority(session, dm_conversation.authority_domain)
    if payload.client_nonce is not None:
        nonce_lock = int.from_bytes(
            hashlib.blake2b(
                (
                    f"{channel.id}@{channel.origin_domain}:"
                    f"{auth.user.id}@{auth.user.origin_domain}:{payload.client_nonce}"
                ).encode(),
                digest_size=8,
            ).digest(),
            byteorder="big",
            signed=True,
        )
        await session.execute(select(func.pg_advisory_xact_lock(nonce_lock)))
        existing = await session.scalar(
            select(Message).where(
                Message.channel_id == channel.id,
                Message.channel_domain == channel.origin_domain,
                Message.author_id == auth.user.id,
                Message.author_domain == auth.user.origin_domain,
                Message.client_nonce == payload.client_nonce,
            )
        )
        if existing is not None:
            return await render_message_payload(session, existing, auth.user)
    referenced: Message | None = None
    referenced_ref: tuple[int, str] | None = None
    if payload.referenced_message_id is not None:
        referenced_ref = payload.referenced_message_id.resolve(settings.domain)
        referenced = await session.scalar(
            select(Message).where(
                Message.id == referenced_ref[0],
                Message.origin_domain == referenced_ref[1],
                Message.channel_id == channel.id,
                Message.channel_domain == channel.origin_domain,
            )
        )
        if referenced is None and not opaque_dm_history_ref_allowed(
            dm_conversation,
            referenced_ref,
            participant_domains={participant.origin_domain for participant in access.participants},
            local_domain=settings.domain,
            remote_available=await dm_authority_history_available(
                session,
                dm_conversation,
                local_domain=settings.domain,
            ),
        ):
            raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    mentions = await resolve_message_create_mentions(
        session,
        redis,
        settings,
        access,
        auth,
        payload,
        admission_options,
        actor_permissions=actor_permissions,
        application_ref=application_ref,
        referenced=referenced,
        poll_result_source=poll_result_source,
    )
    explicit_mention_pairs = mentions.explicit_recipients
    mention_pairs = mentions.recipients
    role_mention_pairs = mentions.role_recipients
    stored_role_mentions = mentions.roles
    stored_mention_everyone = mentions.everyone
    automod_post_commit = None
    if (
        access.guild is not None
        and access.guild.origin_domain == settings.domain
        and admission_options.webhook_id is None
        and not admission_options.automod_already_evaluated
        and poll_result_source is None
    ):
        automod_post_commit = await evaluate_automod_message(
            session,
            redis,
            settings,
            snowflake,
            access.guild,
            channel,
            admission_options.automod_actor or auth.user,
            message_automod_text(
                payload.content,
                poll=payload.poll,
                components=payload.components,
            ),
            mention_count=len(mention_pairs),
            actor_permissions=Permission(
                admission_options.automod_permissions
                if admission_options.automod_permissions is not None
                else actor_permissions
            ),
        )
    attachments = await prepare_message_create_attachments(
        session,
        settings,
        auth.user,
        channel,
        payload,
        admission_options,
    )
    replicated_attachment_payloads = attachments.replicated
    message_attachments = attachments.local
    if poll_result_source is None:
        require_message_encryption_policy(
            channel,
            content=payload.content,
            e2ee=payload.e2ee,
            attachment_count=(
                len(replicated_attachment_payloads)
                if replicated_attachment_payloads
                else len(message_attachments)
            ),
            allow_required_e2ee_starter=(
                admission_options.allow_required_e2ee_starter
                and channel.type in THREAD_CHANNEL_TYPES
                and int(getattr(channel, "message_count", 0) or 0) == 0
            ),
        )
    if channel.encryption_mode == "e2ee" and poll_result_source is None:
        await require_owned_e2ee_sender_device(
            session,
            auth.user,
            payload.e2ee,
            authority_domain=settings.domain,
            channel=channel,
            bot_installation_id=admission_options.bot_installation_id,
            bot_user_installation_id=admission_options.bot_user_installation_id,
            bot_dm_capability_id=admission_options.bot_dm_capability_id,
            bot_worker_id=admission_options.bot_worker_id,
            webhook_id=admission_options.webhook_id,
            webhook_domain=(settings.domain if admission_options.webhook_id is not None else None),
            webhook_e2ee_device_id=admission_options.webhook_e2ee_device_id,
        )
    if (
        channel.encryption_mode == "e2ee"
        and poll_result_source is None
        and (
            not isinstance(payload.e2ee, dict)
            or payload.e2ee.get("operation") != "create"
            or "target_message" in payload.e2ee
        )
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_INVALID"})
    expected_attachment_mode = (
        "plaintext"
        if poll_result_source is not None
        else "e2ee"
        if channel.encryption_mode == "e2ee"
        else "plaintext"
    )
    attachment_modes = (
        [item.get("encryption_mode", "plaintext") for item in replicated_attachment_payloads]
        if replicated_attachment_payloads
        else [item.encryption_mode for item in message_attachments]
    )
    if any(mode != expected_attachment_mode for mode in attachment_modes):
        raise HTTPException(
            status_code=409,
            detail={"code": "MESSAGE_ENCRYPTION_POLICY_INVALID"},
        )
    if poll_result_source is None:
        require_encrypted_rich_admission(
            payload.e2ee,
            author=auth.user,
            attachments=replicated_attachment_payloads or message_attachments,
            mention_refs=mention_pairs,
            sticker_items=sticker_items,
            referenced_message_ref=referenced_ref,
            application_ref=application_ref,
            installation_lineage=message_view_lineage,
            has_controls=bool(encrypted_controls),
            tts=requested_tts,
            voice_message=payload.voice_message,
            flags=payload.flags,
            view_persistent=effective_view_persistent,
            view_version=1 if encrypted_controls else 0,
            forwarded_message_ref=forwarded_ref,
            forwarded_channel_ref=forwarded_channel_ref,
            forward_source_projection_digest=forward_source_projection_digest,
            forwarded_created_at=forwarded_created_at,
            forwarded_edited_at=forwarded_edited_at,
            forwarded_flags=forwarded_flags,
            forwarded_message_type=forwarded_message_type,
        )
    destination_attachments = replicated_attachment_payloads or message_attachments
    if forwarded_ref is not None:
        require_reencrypted_forward_attachments(
            source_attachment_count,
            destination_attachments,
        )
    if (
        forward_snapshot is not None
        and not encrypted_forward_source
        and not replicated_attachment_payloads
    ):
        try:
            forward_snapshot = rebind_forward_snapshot_attachments(
                forward_snapshot,
                message_attachments,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_FORWARD_ATTACHMENT_MISMATCH"},
            ) from exc
    if forward_snapshot is not None and not forward_snapshot_matches_attachments(
        forward_snapshot,
        destination_attachments,
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_FORWARD_ATTACHMENT_MISMATCH"},
        )
    await enforce_message_create_slowmode(
        redis,
        access,
        channel,
        auth.user,
        actor_permissions,
    )
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        return await proxy_remote_guild_message_create(
            session,
            redis,
            settings,
            response_status,
            access,
            auth.user,
            channel,
            payload,
            admission_options,
            FederatedMessageCreateProjection(
                requested_tts=requested_tts,
                sticker_items=sticker_items,
                expression_authorizations=expression_authorizations,
                forwarded_ref=forwarded_ref,
                forwarded_channel_ref=forwarded_channel_ref,
                forward_snapshot=forward_snapshot,
                forward_source_nsfw=forward_source_nsfw,
                forward_source_proof=forward_source_proof,
                application_ref=application_ref,
                message_view_lineage=message_view_lineage,
                effective_view_timeout=effective_view_timeout,
                effective_view_persistent=effective_view_persistent,
                referenced=referenced,
                explicit_mention_pairs=explicit_mention_pairs,
                message_attachments=message_attachments,
                encrypted_poll=encrypted_poll,
                encrypted_rich=encrypted_rich,
                has_message_view=has_message_view,
            ),
        )
    if thread_was_unarchived:
        # Only a guild's home may advance structural thread state.  In
        # particular, keep an archived replica unchanged while an attachment
        # create records its durable media recipient before the synchronous
        # proxy request: that pre-I/O commit must not persist a speculative
        # local unarchive when the authority rejects the proposal.
        channel.archived = False
        channel.archive_timestamp = datetime.now(UTC)
    message_id = (
        admission_options.forced_message_id
        if admission_options.forced_message_id is not None
        else await snowflake.mint()
    )
    if admission_options.forced_message_id is not None:
        existing_forced_message = await session.get(Message, (message_id, settings.domain))
        if existing_forced_message is not None:
            raise HTTPException(status_code=409, detail={"code": "MESSAGE_ID_CONFLICT"})
    if referenced_ref is not None and referenced_ref >= (message_id, settings.domain):
        raise HTTPException(status_code=400, detail={"code": "INVALID_MESSAGE_REFERENCE"})
    admitted_message_type = (
        POLL_RESULT_MESSAGE_TYPE
        if poll_result_source is not None
        else admission_options.interaction_message_type
        if admission_options.interaction_message_type is not None
        else 19
        if referenced_ref is not None
        else 0
    )
    try:
        interaction_metadata = validate_interaction_metadata(
            admission_options.interaction_metadata,
            message_type=admitted_message_type,
            application_ref=application_ref,
            referenced_message_ref=referenced_ref,
            message_ref=(message_id, settings.domain),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_METADATA_INVALID"},
        ) from exc
    mention_refs = [
        {"id": str(user_id), "origin_domain": domain} for user_id, domain in mention_pairs
    ]
    mention_role_refs = [
        {"id": str(role_id), "origin_domain": domain} for role_id, domain in stored_role_mentions
    ]
    dm_history_changed = False
    if access.guild is None:
        conversation = dm_conversation
        if conversation is None:
            raise RuntimeError("direct-message channel has no conversation")
        try:
            history_before = (
                conversation.history_truncated,
                conversation.history_truncated_before_id,
                conversation.history_truncated_before_domain,
                conversation.history_cache_start_id,
                conversation.history_cache_start_domain,
            )
            await admit_federated_dm_message(
                session,
                settings,
                conversation,
                message_id=message_id,
                message_domain=settings.domain,
                delta=dm_message_storage_delta(
                    content=payload.content,
                    e2ee=payload.e2ee,
                    mention_user_refs=mention_refs,
                    mention_role_refs=mention_role_refs,
                    mention_everyone=stored_mention_everyone,
                    attachments=message_attachments,
                    client_nonce=payload.client_nonce,
                    forwarded_message_ref=forwarded_ref,
                    forward_snapshot=forward_snapshot,
                    poll_result=admission_options.poll_result,
                    embeds=[
                        item.model_dump(mode="json", exclude_none=True) for item in payload.embeds
                    ],
                    components=[
                        item.model_dump(mode="json", exclude_none=True)
                        for item in payload.components
                    ],
                    poll=(
                        payload.poll.model_dump(mode="json", exclude_none=True)
                        if payload.poll is not None
                        else encrypted_poll
                    ),
                    sticker_items=sticker_items,
                    application_ref=application_ref,
                    interaction_metadata=interaction_metadata,
                    view_version=(1 if has_message_view and application_ref is not None else 0),
                    view_persistent=effective_view_persistent,
                    view_expires_at=message_view_expires_at,
                ),
                protected_refs=(
                    {reference for reference in (referenced_ref,) if reference is not None} or None
                ),
            )
            dm_history_changed = history_before != (
                conversation.history_truncated,
                conversation.history_truncated_before_id,
                conversation.history_truncated_before_domain,
                conversation.history_cache_start_id,
                conversation.history_cache_start_domain,
            )
        except FederatedDMQuotaExceeded as exc:
            raise HTTPException(status_code=507, detail=exc.detail()) from exc
    message = (
        await session.scalars(
            insert(Message)
            .values(
                id=message_id,
                origin_domain=settings.domain,
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                author_id=auth.user.id,
                author_domain=auth.user.origin_domain,
                content=payload.content,
                e2ee=payload.e2ee,
                embeds=[
                    {
                        **item.model_dump(mode="json", exclude_none=True),
                        **({"type": "poll_result"} if poll_result_source is not None else {}),
                    }
                    for item in payload.embeds
                ],
                components=[
                    item.model_dump(mode="json", exclude_none=True) for item in payload.components
                ],
                # Encrypted sticker metadata stays in the MLS body. The
                # authority retains only the authenticated routing refs.
                sticker_items=[] if encrypted_rich else sticker_items,
                application_id=application_ref[0] if application_ref is not None else None,
                application_domain=application_ref[1] if application_ref is not None else None,
                interaction_metadata=interaction_metadata,
                webhook_id=admission_options.webhook_id,
                webhook_domain=(
                    settings.domain if admission_options.webhook_id is not None else None
                ),
                webhook_name=admission_options.webhook_name,
                webhook_avatar_hash=admission_options.webhook_avatar_hash,
                webhook_avatar_url=admission_options.webhook_avatar_url,
                message_type=admitted_message_type,
                tts=requested_tts,
                view_version=1 if has_message_view and application_ref is not None else 0,
                forwarded_message_id=forwarded_ref[0] if forwarded_ref is not None else None,
                forwarded_message_domain=forwarded_ref[1] if forwarded_ref is not None else None,
                forwarded_channel_id=(
                    forwarded_channel_ref[0] if forwarded_channel_ref is not None else None
                ),
                forwarded_channel_domain=(
                    forwarded_channel_ref[1] if forwarded_channel_ref is not None else None
                ),
                forward_snapshot=forward_snapshot,
                poll_result=admission_options.poll_result,
                encryption_policy_generation=channel.encryption_policy_generation,
                encryption_epoch=channel.encryption_epoch,
                client_nonce=payload.client_nonce,
                referenced_message_id=(referenced_ref[0] if referenced_ref is not None else None),
                referenced_message_domain=(
                    referenced_ref[1] if referenced_ref is not None else None
                ),
                mention_user_refs=mention_refs,
                mention_role_refs=mention_role_refs,
                mention_everyone=stored_mention_everyone,
                flags=(0 if actor_permissions & Permission.EMBED_LINKS else 4)
                | (
                    payload.flags
                    & (
                        MESSAGE_FLAG_SUPPRESS_EMBEDS
                        | MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS
                        | MESSAGE_FLAG_IS_COMPONENTS_V2
                    )
                )
                | admission_options.message_flags
                | inferred_message_shape_flags(
                    voice_message=payload.voice_message,
                    components_v2=(
                        uses_components_v2(payload.components)
                        or bool(encrypted_rich and payload.flags & MESSAGE_FLAG_IS_COMPONENTS_V2)
                    ),
                )
                | (
                    MESSAGE_FLAG_HAS_SNAPSHOT
                    if forward_snapshot is not None or encrypted_forward
                    else 0
                ),
            )
            .returning(Message)
        )
    ).one()
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
    message_view: MessageView | None = None
    if has_message_view and application_ref is not None:
        if message_view_lineage is None:
            raise RuntimeError("interactive message lost its installation lineage")
        integration_type, installation_id, installation_domain, installation_revision = (
            message_view_lineage
        )
        message_view = MessageView(
            message_id=message.id,
            message_domain=message.origin_domain,
            application_id=application_ref[0],
            application_domain=application_ref[1],
            integration_type=integration_type,
            installation_id=installation_id,
            installation_domain=installation_domain,
            installation_revision=installation_revision,
            version=1,
            persistent=effective_view_persistent,
            expires_at=(None if effective_view_persistent else message_view_expires_at),
        )
        session.add(message_view)
    stored_poll_payload: dict[str, object] | None = None
    if payload.poll is not None or encrypted_poll is not None:
        await session.flush()
        stored_poll_payload = await render_poll_payload(session, message, viewer=auth.user)
    if replicated_attachment_payloads:
        try:
            message_attachments = await replicate_message_attachments(
                session,
                settings,
                message,
                auth.user,
                replicated_attachment_payloads,
            )
        except ValueError:
            raise HTTPException(
                status_code=400, detail={"code": "FEDERATED_ATTACHMENT_INVALID"}
            ) from None
        if len(message_attachments) != len(replicated_attachment_payloads):
            raise HTTPException(status_code=410, detail={"code": "ATTACHMENT_DELETED"})
    added_thread_members: list[ThreadMember] = []
    thread_rekeyed = False
    if channel.type in THREAD_CHANNEL_TYPES:
        # A two-phase encrypted forum starter deliberately reuses the thread
        # snowflake after newer MLS controls have been written. Never regress
        # the public cursor behind the control log during that atomic claim.
        advance_thread_message_projection(channel, message)
        if not admission_options.mark_thread_starter:
            channel.message_count = int(getattr(channel, "message_count", 0) or 0) + 1
            channel.total_message_sent = int(getattr(channel, "total_message_sent", 0) or 0) + 1
        if access.guild is None:
            raise RuntimeError("guild thread has no guild")
        (
            added_thread_members,
            thread_rekeyed,
            failed_role_mentions,
        ) = await admit_thread_message_members(
            session,
            redis,
            settings,
            access.guild,
            channel,
            auth.user,
            actor_permissions,
            mention_pairs,
            role_mention_pairs,
            admit_actor=admission_options.webhook_id is None,
        )
        if failed_role_mentions:
            message.flags |= MESSAGE_FLAG_FAILED_TO_MENTION_SOME_ROLES_IN_THREAD
        if admission_options.mark_thread_starter:
            channel.starter_message_id = message.id
            channel.starter_message_domain = message.origin_domain
        if admission_options.queue_thread_create:
            if access.guild is None or access.guild.origin_domain != settings.domain:
                raise RuntimeError("thread creation must be committed by its guild home")
            initial_thread_state = federation_channel_state(channel)
            # The starter message is the next ordered guild event and may not
            # exist on a replica yet. Publish a valid empty thread first; the
            # message event binds the starter identity atomically at ingest.
            if admission_options.mark_thread_starter:
                initial_thread_state.update(
                    {
                        "starter_message_id": None,
                        "starter_message_domain": None,
                        "message_count": 0,
                        "total_message_sent": 0,
                    }
                )
            await queue_guild_mutation(
                session,
                settings,
                access.guild,
                auth.user,
                "guild.channel.create",
                {"channel": initial_thread_state},
                channel=channel,
            )
    for attachment in message_attachments:
        attachment.message_id = message.id
        attachment.message_domain = message.origin_domain
    remote_destinations: set[str] = set()
    if access.guild is None:
        conversation = dm_conversation
        if conversation is None:
            raise RuntimeError("direct-message channel has no conversation")
        message_content = {
            "message": message_payload(
                message,
                auth.user,
                message_attachments,
                poll=stored_poll_payload,
                view=message_view,
                include_forward_source=True,
            ),
            "author": profile_from_user(auth.user),
            **(
                {
                    "forward_source_nsfw": forward_source_nsfw,
                    "forward_source_proof": forward_source_proof,
                }
                if forwarded_ref is not None
                else {}
            ),
        }
        cast(dict[str, object], message_content["message"])["attachments"] = [
            federation_attachment_payload(item) for item in message_attachments
        ]
        if conversation.type == "group" and conversation.authority_domain != settings.domain:
            remote_destinations = {conversation.authority_domain}
            envelope = await build_envelope(
                session,
                settings,
                "dm.group.message.proposed",
                auth.user,
                message_content,
                context={
                    "conversation_id": str(conversation.id),
                    "conversation_domain": conversation.origin_domain,
                    "state_version": str(conversation.state_version),
                },
            )
        else:
            remote_destinations = {
                participant.origin_domain
                for participant in access.participants
                if participant.origin_domain != settings.domain
            }
            envelope = await build_envelope(
                session,
                settings,
                (
                    "dm.group.message.committed"
                    if conversation.type == "group"
                    else "dm.message.create"
                ),
                auth.user,
                message_content,
                context=(
                    {
                        "conversation_id": str(conversation.id),
                        "conversation_domain": conversation.origin_domain,
                        "state_version": str(conversation.state_version),
                    }
                    if conversation.type == "group"
                    else None
                ),
                authority_attested_actor=(
                    auth.user.origin_domain != settings.domain
                    and conversation.authority_domain == settings.domain
                    and (poll_result_source is not None or conversation.type == "group")
                ),
            )
        for destination in remote_destinations:
            await queue_event(session, settings, destination, envelope)
    elif access.guild.origin_domain == settings.domain:
        if thread_was_unarchived or thread_rekeyed:
            if prior_thread_message_projection is None:
                raise RuntimeError("thread message projection was not captured")
            await queue_guild_mutation(
                session,
                settings,
                access.guild,
                auth.user,
                "guild.channel.update",
                {
                    "channel": thread_structural_state_before_message(
                        channel,
                        prior_thread_message_projection,
                    )
                },
                channel=channel,
            )
        remote_destinations = await remote_destinations_with_channel_access(
            session, settings, access.guild, channel
        )
        if remote_destinations:
            seq = await assign_guild_sequence(session, access.guild)
            guild_event_signer = await guild_mutation_signer(
                session,
                settings,
                access.guild,
                auth.user,
            )
            envelope = await build_guild_authority_envelope(
                session,
                settings,
                access.guild,
                "guild.message.create",
                guild_event_signer,
                {
                    "message": message_payload(
                        message,
                        auth.user,
                        message_attachments,
                        poll=stored_poll_payload,
                        view=message_view,
                        include_forward_source=True,
                    ),
                    "author": profile_from_user(auth.user),
                    "thread_starter": admission_options.mark_thread_starter,
                },
                context={
                    "guild_id": str(access.guild.id),
                    "guild_domain": access.guild.origin_domain,
                    "seq": str(seq),
                },
            )
            store_guild_event(
                session,
                access.guild,
                seq,
                str(envelope["event_id"]),
                envelope,
            )
            for destination in remote_destinations:
                await queue_event(session, settings, destination, envelope)
    await mark_guild_activity(session, settings, access, auth.user)
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            mention_user_refs=mention_refs,
        )
    )
    result = message_payload(
        message,
        auth.user,
        message_attachments,
        poll=stored_poll_payload,
        view=message_view,
    )
    if access.guild is None:
        result["delivery_status"] = "pending" if remote_destinations else "delivered"
    rendered_thread: dict[str, object] | None = None
    if channel.type in THREAD_CHANNEL_TYPES:
        # ``updated_at`` is database-generated for an UPDATE and SQLAlchemy
        # expires it after the flush. Materialize and render the complete
        # thread projection while async I/O is still legal; post-commit
        # gateway fanout must never lazy-load ORM state.
        await materialize_updated_at(session, channel)
        rendered_thread = channel_payload(channel)
    postcommit = MessageCreatePostCommit(
        automod=automod_post_commit,
        access=access,
        message=message,
        result=result,
        attachments=message_attachments,
        remote_destinations=remote_destinations,
        mark_thread_starter=admission_options.mark_thread_starter,
        defer_dispatch=admission_options.defer_dispatch,
        thread_was_unarchived=thread_was_unarchived,
        added_thread_members=added_thread_members,
        dm_history_changed=dm_history_changed,
        dm_conversation=dm_conversation,
        rendered_thread=rendered_thread,
    )
    if admission_options.transaction is not None:
        admission_options.transaction.stage(postcommit)
    else:
        await session.commit()
        await postcommit.publish(session, redis, settings)
    return result


@router.patch("/{channel_id}/messages/{message_id}")
async def edit_message(
    channel_id: EntityRef,
    message_id: EntityRef,
    payload: MessageEdit,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    mutation_options: MessageMutationOptions = Depends(default_message_mutation_options),
) -> dict[str, object]:
    if not isinstance(mutation_options, MessageMutationOptions):
        mutation_options = default_message_mutation_options()
    access = (
        await load_webhook_capability_channel_access(
            session,
            settings,
            channel_id,
            webhook_channel_id=cast(int, mutation_options.webhook_channel_id),
            webhook_channel_domain=cast(str, mutation_options.webhook_channel_domain),
        )
        if mutation_options.webhook_id is not None
        else await load_channel_access(session, settings, auth.user, channel_id)
    )
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        result = await proxy_remote_guild_message_operation(
            session,
            settings,
            access,
            auth.user,
            "message.edit",
            message_ref=message_id,
            edit=payload,
            mutation_options=mutation_options,
            redis=redis,
        )
        rendered = result.get("message")
        if not isinstance(rendered, dict):
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
            )
        return {str(key): value for key, value in rendered.items()}
    if access.guild is None and access.channel.origin_domain != settings.domain:
        result = await proxy_remote_dm_message_operation(
            session,
            settings,
            access,
            auth.user,
            "message.edit",
            message_id,
            edit=payload,
            mutation_options=mutation_options,
        )
        rendered = result.get("message")
        if not isinstance(rendered, dict):
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
            )
        return {str(key): value for key, value in rendered.items()}
    require_local_mutation_authority(access, settings)
    if access.guild is not None and access.channel.type == 5:
        await lock_announcement_mutation(session)
    if access.guild is not None:
        await lock_terminal_room(
            session,
            "guild",
            access.guild.id,
            access.guild.origin_domain,
        )
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    require_unarchived_thread(channel)
    actor_permissions = (
        WEBHOOK_CAPABILITY_MESSAGE_PERMISSIONS
        if mutation_options.webhook_id is not None
        else await require_channel_permissions(
            session,
            redis,
            access,
            auth.user,
            required_permissions("message.edit.self"),
        )
    )
    if (
        channel.type in THREAD_CHANNEL_TYPES
        and bool(channel.locked)
        and not actor_permissions & Permission.MANAGE_THREADS
    ):
        raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})
    await require_dm_send(session, access, auth.user)
    message = await channel_message(
        session,
        settings,
        channel,
        message_id,
        for_update=True,
        require_active=True,
    )
    if mutation_options.webhook_id is not None:
        if (message.webhook_id, message.webhook_domain) != (
            mutation_options.webhook_id,
            settings.domain,
        ):
            raise HTTPException(status_code=404, detail={"code": "WEBHOOK_MESSAGE_NOT_FOUND"})
    elif (message.author_id, message.author_domain) != (auth.user.id, auth.user.origin_domain):
        # Moderation is intentionally delete-only. Editing another user's
        # content while preserving their authorship would be impersonation.
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    await require_editable_message(session, message)
    if access.guild is not None and mutation_options.webhook_id is None:
        await require_member_interactions_allowed(
            session,
            access.guild,
            auth.user,
            Permission.SEND_MESSAGES,
        )
    if mutation_options.expression_authorization_checked and payload.expression_actor_intents:
        raise HTTPException(
            status_code=400,
            detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
        )
    prospective_content = (
        payload.content if "content" in payload.model_fields_set else message.content
    )
    prospective_e2ee = payload.e2ee if "e2ee" in payload.model_fields_set else message.e2ee
    prospective_embeds: Sequence[object] = (
        list(payload.embeds or [])
        if "embeds" in payload.model_fields_set
        else list(message.embeds or [])
    )
    prospective_components: Sequence[object] = (
        list(payload.components or [])
        if "components" in payload.model_fields_set
        else list(message.components or [])
    )
    encrypted_contract, encrypted_controls, encrypted_poll = encrypted_rich_routing(
        prospective_e2ee
    )
    encrypted_rich_edit = (
        "e2ee" in payload.model_fields_set
        and isinstance(prospective_e2ee, dict)
        and "rich_payload_digest" in prospective_e2ee
    )
    if encrypted_rich_edit and encrypted_poll is not None:
        raise HTTPException(
            status_code=400,
            detail={"code": "POLL_EDIT_UNSUPPORTED"},
        )
    if (
        not mutation_options.expression_authorization_checked
        and access.guild is not None
        and mutation_options.webhook_id is None
    ):
        raw_edit_sticker_refs = (
            prospective_e2ee.get("message_sticker_refs", [])
            if encrypted_rich_edit and isinstance(prospective_e2ee, dict)
            else []
        )
        if not isinstance(raw_edit_sticker_refs, list):
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
            )
        try:
            edit_sticker_refs = [EntityRef(str(item)) for item in raw_edit_sticker_refs]
            edit_expression_tokens = expression_custom_emoji_tokens(
                content=(payload.content if "content" in payload.model_fields_set else None),
                components=(
                    payload.components if "components" in payload.model_fields_set else None
                ),
                poll=None,
                e2ee=(payload.e2ee if "e2ee" in payload.model_fields_set else None),
                default_domain=access.guild.origin_domain,
            )
            application_ref = (
                (mutation_options.application_id, mutation_options.application_domain)
                if mutation_options.application_id is not None
                and mutation_options.application_domain is not None
                else (
                    await expression_application_ref_for_actor(session, auth.user)
                    if edit_expression_tokens
                    or edit_sticker_refs
                    or payload.expression_actor_intents
                    else None
                )
            )
            operation_id = hashlib.sha256(
                f"message.edit\n{message.id}@{message.origin_domain}".encode()
            ).hexdigest()
            (
                expression_authorizations,
                expression_sticker_items,
            ) = await acquire_expression_use_authorizations(
                session,
                redis,
                settings,
                auth.user,
                application_ref=application_ref,
                actor_intents=payload.expression_actor_intents,
                target_guild_ref=f"{access.guild.id}@{access.guild.origin_domain}",
                target_channel_ref=f"{channel.id}@{channel.origin_domain}",
                target_message_ref=f"{message.id}@{message.origin_domain}",
                operation="message.edit",
                operation_id=operation_id,
                emoji_tokens=edit_expression_tokens,
                sticker_refs=edit_sticker_refs,
            )
            attested_tokens, attested_items = await validate_expression_authorization_map(
                session,
                redis,
                settings,
                expression_authorizations,
                requester_ref=f"{auth.user.id}@{auth.user.origin_domain}",
                requester_type=cast(Literal["human", "bot"], auth.user.account_type),
                application_ref=(
                    f"{application_ref[0]}@{application_ref[1]}"
                    if application_ref is not None
                    else None
                ),
                target_guild_ref=f"{access.guild.id}@{access.guild.origin_domain}",
                target_channel_ref=f"{channel.id}@{channel.origin_domain}",
                target_message_ref=f"{message.id}@{message.origin_domain}",
                operation="message.edit",
                operation_id=operation_id,
                emoji_tokens=edit_expression_tokens,
                sticker_items=expression_sticker_items,
            )
            await validate_attested_expression_target(
                session,
                auth.user,
                access.guild,
                int(actor_permissions),
                attested_tokens,
                attested_items,
            )
            mutation_options = replace(
                mutation_options,
                expression_authorization_checked=True,
                attested_expression_tokens=tuple(attested_tokens),
                attested_expression_sticker_items=tuple(attested_items),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            ) from exc
    elif payload.expression_actor_intents:
        raise HTTPException(
            status_code=400,
            detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
        )
    added_attachments: list[Attachment] = []
    removed_attachments: list[Attachment] = []
    current_attachments: list[Attachment] = []
    prospective_attachment_refs: set[tuple[int, str]]
    if payload.attachment_ids is not None:
        if not actor_permissions & Permission.ATTACH_FILES:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
        if mutation_options.authoritative_attachment_refs is not None:
            requested_refs = list(mutation_options.authoritative_attachment_refs)
            if (
                len(requested_refs) != len(payload.attachment_ids)
                or [item[0] for item in requested_refs]
                != [int(item) for item in payload.attachment_ids]
                or len(requested_refs) != len(set(requested_refs))
            ):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "FEDERATED_ATTACHMENT_INVALID"},
                )
        else:
            requested_refs = [(int(item), settings.domain) for item in payload.attachment_ids]
        prospective_attachment_refs = set(requested_refs)
        current_refs = set(
            (
                await session.execute(
                    select(Attachment.id, Attachment.origin_domain).where(
                        Attachment.message_id == message.id,
                        Attachment.message_domain == message.origin_domain,
                        Attachment.deleted_at.is_(None),
                    )
                )
            ).tuples()
        )
        for attachment_id, attachment_domain in sorted(
            current_refs | prospective_attachment_refs,
            key=lambda item: (item[1], item[0]),
        ):
            await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
        current_attachments = list(
            await session.scalars(
                select(Attachment)
                .where(
                    Attachment.message_id == message.id,
                    Attachment.message_domain == message.origin_domain,
                    Attachment.deleted_at.is_(None),
                )
                .with_for_update()
            )
        )
        current_by_ref = {(item.id, item.origin_domain): item for item in current_attachments}
        missing_remote_refs = {
            item
            for item in prospective_attachment_refs - set(current_by_ref)
            if item[1] != settings.domain
        }
        replicated_payloads = list(mutation_options.replicated_attachments)
        try:
            replicated_refs = {
                (
                    int(str(item["id"])),
                    normalize_domain(str(item["origin_domain"])),
                )
                for item in replicated_payloads
            }
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail={"code": "FEDERATED_ATTACHMENT_INVALID"},
            ) from None
        if replicated_refs != missing_remote_refs or len(replicated_refs) != len(
            replicated_payloads
        ):
            raise HTTPException(
                status_code=400,
                detail={"code": "FEDERATED_ATTACHMENT_INVALID"},
            )
        if replicated_payloads:
            try:
                replicated = await replicate_message_attachments(
                    session,
                    settings,
                    message,
                    auth.user,
                    replicated_payloads,
                )
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "FEDERATED_ATTACHMENT_INVALID"},
                ) from None
            if len(replicated) != len(replicated_payloads):
                raise HTTPException(status_code=410, detail={"code": "ATTACHMENT_DELETED"})
            current_by_ref.update({(item.id, item.origin_domain): item for item in replicated})
        for attachment_id, attachment_domain in sorted(
            prospective_attachment_refs,
            key=lambda item: (item[1], item[0]),
        ):
            attachment_ref = (attachment_id, attachment_domain)
            if attachment_ref in current_by_ref:
                continue
            attachment = await finalize_attachment(
                session,
                settings,
                auth.user,
                attachment_id,
                required_purpose=mutation_options.required_attachment_purpose,
            )
            if attachment_domain != attachment.origin_domain:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "ATTACHMENT_NOT_FOUND"},
                )
            require_available_edit_attachment(attachment, channel, mutation_options)
            attachment.asset_binding = None
            attachment.message_id = message.id
            attachment.message_domain = message.origin_domain
            added_attachments.append(attachment)
        for attachment in current_attachments:
            if (attachment.id, attachment.origin_domain) not in prospective_attachment_refs:
                await discard_attachment(session, settings, attachment)
                removed_attachments.append(attachment)
    else:
        prospective_attachment_refs = set(
            (
                await session.execute(
                    select(Attachment.id, Attachment.origin_domain).where(
                        Attachment.message_id == message.id,
                        Attachment.message_domain == message.origin_domain,
                        Attachment.deleted_at.is_(None),
                    )
                )
            ).tuples()
        )
    effective_attachments = list(
        await session.scalars(
            select(Attachment).where(
                Attachment.message_id == message.id,
                Attachment.message_domain == message.origin_domain,
                Attachment.deleted_at.is_(None),
            )
        )
    )
    try:
        validate_attachment_url_references(
            embeds=prospective_embeds,
            components=prospective_components,
            attachments=effective_attachments,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "ATTACHMENT_REFERENCE_INVALID"},
        ) from exc
    validate_merged_message_edit(
        content=prospective_content,
        e2ee=prospective_e2ee,
        embeds=prospective_embeds,
        components=prospective_components,
        attachment_count=len(prospective_attachment_refs),
        sticker_items=list(message.sticker_items or []),
        forward_snapshot=(
            dict(message.forward_snapshot) if isinstance(message.forward_snapshot, dict) else None
        ),
        current_flags=int(message.flags or 0),
        requested_flags=payload.flags,
    )
    require_message_encryption_policy(
        channel,
        content=prospective_content,
        e2ee=prospective_e2ee,
        attachment_count=len(prospective_attachment_refs),
    )
    if channel.encryption_mode == "e2ee":
        await require_owned_e2ee_sender_device(
            session,
            auth.user,
            prospective_e2ee,
            authority_domain=settings.domain,
            channel=channel,
            bot_installation_id=mutation_options.bot_installation_id,
            bot_user_installation_id=mutation_options.bot_user_installation_id,
            bot_dm_capability_id=mutation_options.bot_dm_capability_id,
            bot_worker_id=mutation_options.bot_worker_id,
            webhook_id=mutation_options.webhook_id,
            webhook_domain=(settings.domain if mutation_options.webhook_id is not None else None),
            webhook_e2ee_device_id=mutation_options.webhook_e2ee_device_id,
        )
    if channel.encryption_mode == "e2ee" and (
        not isinstance(prospective_e2ee, dict)
        or prospective_e2ee.get("operation") != "edit"
        or prospective_e2ee.get("target_message") != f"{message.id}@{message.origin_domain}"
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_INVALID"})
    if encrypted_rich_edit:
        try:
            validate_e2ee_message_revision(prospective_e2ee, message.e2ee)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_MESSAGE_REVISION_CONFLICT"},
            ) from exc
    if mutation_options.expression_authorization_checked:
        try:
            checked_tokens = expression_custom_emoji_tokens(
                content=(payload.content if "content" in payload.model_fields_set else None),
                components=(
                    payload.components if "components" in payload.model_fields_set else None
                ),
                poll=None,
                e2ee=(payload.e2ee if "e2ee" in payload.model_fields_set else None),
                default_domain=(
                    access.guild.origin_domain if access.guild is not None else settings.domain
                ),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            ) from exc
        if checked_tokens != list(mutation_options.attested_expression_tokens):
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            )
        encrypted_edit_sticker_items = (
            list(mutation_options.attested_expression_sticker_items)
            if encrypted_rich_edit
            else list(message.sticker_items or [])
        )
    elif encrypted_rich_edit:
        if not isinstance(prospective_e2ee, dict):
            raise RuntimeError("encrypted rich edit lost its envelope")
        raw_custom_emoji_refs = prospective_e2ee.get("message_custom_emoji_refs", [])
        if not isinstance(raw_custom_emoji_refs, list):
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
            )
        await validate_custom_emoji_tokens(
            session,
            auth.user,
            [str(item) for item in raw_custom_emoji_refs],
            target_guild=access.guild,
            target_permissions=actor_permissions,
            trusted_external_domain=mutation_options.trusted_external_domain,
        )
        raw_sticker_refs = prospective_e2ee.get("message_sticker_refs", [])
        if not isinstance(raw_sticker_refs, list):
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
            )
        try:
            encrypted_edit_sticker_refs = [EntityRef(str(item)) for item in raw_sticker_refs]
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
            ) from exc
        encrypted_edit_sticker_items = await resolve_sticker_items(
            session,
            auth.user,
            encrypted_edit_sticker_refs,
            default_domain=(
                access.guild.origin_domain if access.guild is not None else settings.domain
            ),
            target_guild=access.guild,
            target_permissions=actor_permissions,
            maximum=9,
        )
    else:
        encrypted_edit_sticker_items = list(message.sticker_items or [])
    if not mutation_options.expression_authorization_checked:
        await validate_custom_emoji_use(
            session,
            auth.user,
            prospective_content,
            target_guild=access.guild,
            target_permissions=actor_permissions,
            trusted_external_domain=mutation_options.trusted_external_domain,
        )
        await resolve_rich_custom_emojis(
            session,
            auth.user,
            components=(payload.components if "components" in payload.model_fields_set else None),
            poll=None,
            default_domain=settings.domain,
            target_guild=access.guild,
            target_permissions=actor_permissions,
            trusted_external_domain=mutation_options.trusted_external_domain,
        )
    await validate_custom_sticker_use(
        session,
        auth.user,
        prospective_content,
        target_guild=access.guild,
        target_permissions=actor_permissions,
    )
    authoritative_mention_pairs: list[tuple[int, str]] | None = None
    authoritative_mention_refs: list[dict[str, str]] | None = None
    authoritative_role_pairs: list[tuple[int, str]] | None = None
    authoritative_mention_everyone: bool | None = None
    encrypted_edit_mentions: list[tuple[int, str]] | None = None
    referenced_for_mentions = (
        await session.get(
            Message,
            (message.referenced_message_id, message.referenced_message_domain),
        )
        if message.referenced_message_id is not None
        and message.referenced_message_domain is not None
        and (
            encrypted_rich_edit
            or bool({"content", "components", "allowed_mentions"} & payload.model_fields_set)
        )
        else None
    )
    if encrypted_rich_edit:
        encrypted_edit_mentions = await resolve_encrypted_rich_mentions(
            session,
            access,
            prospective_e2ee,
            actor_permissions=actor_permissions,
            referenced=referenced_for_mentions,
        )
        if encrypted_edit_mentions is None:
            raise RuntimeError("encrypted rich edit lost mention routing")
        if (
            mutation_options.authoritative_mention_refs is not None
            and sorted(mutation_options.authoritative_mention_refs) != encrypted_edit_mentions
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
            )
        authoritative_mention_pairs = encrypted_edit_mentions
        if not isinstance(prospective_e2ee, dict):
            raise RuntimeError("encrypted rich edit lost its envelope")
        authoritative_role_pairs = _encrypted_mention_refs(
            prospective_e2ee,
            "message_mention_role_refs",
        )
        authoritative_mention_everyone = bool(prospective_e2ee["message_mention_everyone"])
    elif mutation_options.authoritative_mention_refs is not None:
        authoritative_mention_pairs = list(
            dict.fromkeys(mutation_options.authoritative_mention_refs)
        )
        authoritative_role_pairs = list(
            dict.fromkeys(mutation_options.authoritative_mention_role_refs or ())
        )
        authoritative_mention_everyone = bool(mutation_options.authoritative_mention_everyone)
    elif {"content", "components", "allowed_mentions"} & payload.model_fields_set:
        resolved_mentions = await resolve_allowed_mentions_projection(
            session,
            redis,
            settings,
            access,
            auth.user,
            regular_message_allowed_mentions(payload.allowed_mentions),
            prospective_content,
            prospective_components,
            actor_permissions=actor_permissions,
            replied_user_ref=(
                (referenced_for_mentions.author_id, referenced_for_mentions.author_domain)
                if referenced_for_mentions is not None
                else None
            ),
        )
        authoritative_mention_pairs = list(resolved_mentions.recipients)
        authoritative_role_pairs = list(resolved_mentions.roles)
        authoritative_mention_everyone = resolved_mentions.everyone
    if authoritative_mention_pairs is not None:
        await require_valid_message_mentions(session, access, authoritative_mention_pairs)
        authoritative_mention_refs = [
            {"id": str(user_id), "origin_domain": domain}
            for user_id, domain in authoritative_mention_pairs
        ]
    app_ref: tuple[int, str] | None = None
    installation_lineage: tuple[str, int, str, int] | None = None
    view: MessageView | None = None
    new_version = 0
    persistent = False
    expires_at: datetime | None = None
    view_edit = "components" in payload.model_fields_set or encrypted_rich_edit
    if view_edit:
        app_ref = (
            (mutation_options.application_id, mutation_options.application_domain)
            if mutation_options.application_id is not None
            and mutation_options.application_domain is not None
            else None
        )
        desired_controls = (
            bool(encrypted_controls) if encrypted_rich_edit else bool(prospective_components)
        )
        if (
            desired_controls
            and app_ref is None
            and not mutation_options.allow_render_only_components
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "COMPONENT_APPLICATION_REQUIRED"},
            )
        if encrypted_rich_edit and app_ref != (
            (message.application_id, message.application_domain)
            if message.application_id is not None and message.application_domain is not None
            else None
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "MESSAGE_APPLICATION_MISMATCH"},
            )
        view = await session.scalar(
            select(MessageView)
            .where(
                MessageView.message_id == message.id,
                MessageView.message_domain == message.origin_domain,
            )
            .with_for_update()
        )
        if view is not None and (view.application_id, view.application_domain) != app_ref:
            raise HTTPException(status_code=403, detail={"code": "MESSAGE_VIEW_NOT_OWNED"})
        current_version = int(message.view_version or 0)
        if view is not None and view.version != current_version:
            raise HTTPException(status_code=409, detail={"code": "MESSAGE_VIEW_VERSION_CONFLICT"})
        if (
            not encrypted_rich_edit
            and payload.view_version is not None
            and payload.view_version != current_version
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "MESSAGE_VIEW_VERSION_CONFLICT", "version": current_version},
            )
        new_version = current_version + 1 if desired_controls or view is not None else 0
        if desired_controls:
            if encrypted_rich_edit and not isinstance(prospective_e2ee, dict):
                raise RuntimeError("encrypted rich edit lost its envelope")
            encrypted_view_persistent = (
                bool(prospective_e2ee.get("view_persistent"))
                if isinstance(prospective_e2ee, dict)
                else False
            )
            persistent = (
                encrypted_view_persistent
                if encrypted_rich_edit
                else (
                    bool(payload.view_persistent)
                    if payload.view_persistent is not None
                    else (bool(view.persistent) if view is not None else False)
                )
            )
            timeout = (
                cast(int, encrypted_contract["view_timeout_seconds"])
                if encrypted_rich_edit and encrypted_contract is not None
                else payload.view_timeout_seconds
            )
            expires_at = (
                None
                if persistent
                else datetime.now(UTC)
                + timedelta(
                    seconds=(
                        timeout
                        or (
                            max(
                                1,
                                int((view.expires_at - datetime.now(UTC)).total_seconds()),
                            )
                            if view is not None and view.expires_at is not None
                            else 900
                        )
                    )
                )
            )
        if app_ref is not None:
            installation_lineage = await message_view_installation_lineage(
                session,
                settings,
                mutation_options,
            )
        if encrypted_rich_edit:
            if payload.flags is None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"},
                )
            effective_mentions = (
                authoritative_mention_pairs
                if authoritative_mention_pairs is not None
                else [
                    (int(str(item["id"])), str(item["origin_domain"]))
                    for item in list(message.mention_user_refs or [])
                    if isinstance(item, dict)
                    and str(item.get("id", "")).isdigit()
                    and isinstance(item.get("origin_domain"), str)
                ]
            )
            previous_envelope = message.e2ee if isinstance(message.e2ee, dict) else {}
            require_encrypted_rich_admission(
                prospective_e2ee,
                author=auth.user,
                attachments=effective_attachments,
                mention_refs=effective_mentions,
                sticker_items=encrypted_edit_sticker_items,
                referenced_message_ref=(
                    (message.referenced_message_id, message.referenced_message_domain)
                    if message.referenced_message_id is not None
                    and message.referenced_message_domain is not None
                    else None
                ),
                application_ref=app_ref,
                installation_lineage=installation_lineage,
                has_controls=desired_controls,
                tts=bool(message.tts),
                voice_message=bool(message.flags & MESSAGE_FLAG_IS_VOICE_MESSAGE),
                flags=payload.flags
                | (
                    MESSAGE_FLAG_IS_VOICE_MESSAGE
                    if message.flags & MESSAGE_FLAG_IS_VOICE_MESSAGE
                    else 0
                ),
                view_persistent=persistent,
                view_version=new_version,
                forwarded_message_ref=(
                    (message.forwarded_message_id, message.forwarded_message_domain)
                    if message.forwarded_message_id is not None
                    and message.forwarded_message_domain is not None
                    else None
                ),
                forwarded_channel_ref=(
                    (message.forwarded_channel_id, message.forwarded_channel_domain)
                    if message.forwarded_channel_id is not None
                    and message.forwarded_channel_domain is not None
                    else None
                ),
                forward_source_projection_digest=cast(
                    str | None,
                    previous_envelope.get("forward_source_projection_digest"),
                ),
                forwarded_created_at=(
                    datetime.fromisoformat(cast(str, previous_envelope["forwarded_created_at"]))
                    if isinstance(previous_envelope.get("forwarded_created_at"), str)
                    else None
                ),
                forwarded_edited_at=(
                    datetime.fromisoformat(cast(str, previous_envelope["forwarded_edited_at"]))
                    if isinstance(previous_envelope.get("forwarded_edited_at"), str)
                    else None
                ),
                forwarded_flags=cast(
                    int | None,
                    previous_envelope.get("forwarded_flags"),
                ),
                forwarded_message_type=cast(
                    int | None,
                    previous_envelope.get("forwarded_message_type"),
                ),
            )
    automod_post_commit = None
    if access.guild is not None and access.guild.origin_domain == settings.domain:
        prospective_poll = (
            await render_poll_payload(session, message) if prospective_e2ee is None else None
        )
        automod_post_commit = await evaluate_automod_message(
            session,
            redis,
            settings,
            snowflake,
            access.guild,
            channel,
            mutation_options.automod_actor or auth.user,
            message_automod_text(
                prospective_content,
                poll=prospective_poll,
                components=prospective_components,
            ),
            mention_count=(
                len(authoritative_mention_pairs)
                if authoritative_mention_pairs is not None
                else len(message.mention_user_refs or [])
            ),
            actor_permissions=Permission(
                mutation_options.automod_permissions
                if mutation_options.automod_permissions is not None
                else actor_permissions
            ),
        )
    if view_edit:
        if desired_controls and app_ref is None:
            raise RuntimeError("validated component edit lost application identity")
        if desired_controls:
            if view is None:
                if installation_lineage is None or app_ref is None:
                    raise RuntimeError("interactive message lost its installation lineage")
                integration_type, installation_id, installation_domain, installation_revision = (
                    installation_lineage
                )
                session.add(
                    MessageView(
                        message_id=message.id,
                        message_domain=message.origin_domain,
                        application_id=app_ref[0],
                        application_domain=app_ref[1],
                        integration_type=integration_type,
                        installation_id=installation_id,
                        installation_domain=installation_domain,
                        installation_revision=installation_revision,
                        version=new_version,
                        persistent=persistent,
                        expires_at=expires_at,
                    )
                )
            else:
                if installation_lineage is None:
                    raise RuntimeError("interactive message lost its installation lineage")
                (
                    view.integration_type,
                    view.installation_id,
                    view.installation_domain,
                    view.installation_revision,
                ) = installation_lineage
                view.version = new_version
                view.persistent = persistent
                view.expires_at = expires_at
            if app_ref is None:
                raise RuntimeError("interactive message lost its application")
            message.application_id = app_ref[0]
            message.application_domain = app_ref[1]
        elif view is not None:
            await session.delete(view)
        message.view_version = new_version
    if "content" in payload.model_fields_set:
        message.content = payload.content
    if "e2ee" in payload.model_fields_set:
        message.e2ee = payload.e2ee
    if payload.embeds is not None:
        message.embeds = [
            item.model_dump(mode="json", exclude_none=True) for item in payload.embeds
        ]
    if payload.components is not None:
        message.components = [
            item.model_dump(mode="json", exclude_none=True) for item in payload.components
        ]
        if uses_components_v2(payload.components):
            message.flags |= MESSAGE_FLAG_IS_COMPONENTS_V2
    if payload.flags is not None:
        editable_flags = MESSAGE_FLAG_SUPPRESS_EMBEDS | MESSAGE_FLAG_IS_COMPONENTS_V2
        message.flags = (message.flags & ~editable_flags) | (payload.flags & editable_flags)
    if authoritative_mention_refs is not None:
        message.mention_user_refs = authoritative_mention_refs
        projection = await session.get(
            MessageProjection,
            (message.id, message.origin_domain),
            with_for_update=True,
        )
        if projection is None:
            session.add(
                MessageProjection(
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    channel_id=message.channel_id,
                    channel_domain=message.channel_domain,
                    mention_user_refs=authoritative_mention_refs,
                )
            )
        else:
            projection.mention_user_refs = authoritative_mention_refs
    if authoritative_role_pairs is not None:
        message.mention_role_refs = [
            {"id": str(role_id), "origin_domain": domain}
            for role_id, domain in authoritative_role_pairs
        ]
    if authoritative_mention_everyone is not None:
        message.mention_everyone = authoritative_mention_everyone
    message.encryption_policy_generation = channel.encryption_policy_generation
    message.encryption_epoch = channel.encryption_epoch
    message.edited_at = datetime.now(UTC)
    announcement_effects: AnnouncementSyncEffects | None = None
    if access.guild is not None and int(getattr(message, "flags", 0) or 0) & (
        MESSAGE_FLAG_CROSSPOSTED
    ):
        announcement_effects = await propagate_announcement_source_change(
            session,
            settings,
            snowflake,
            access.guild,
            message,
            auth.user,
            source_deleted=False,
            changed_at=message.edited_at,
        )
    # An update payload is a complete replacement in both gateway clients and
    # remote projections. Preserve the stored attachment set on content-only
    # edits instead of accidentally serializing it as an empty list.
    result = await render_message_payload(session, message, auth.user)
    dm_federation_destinations: set[str] = set()
    if access.guild is not None:
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            auth.user,
            "guild.message.update",
            {"message": result},
            channel=channel,
        )
    else:
        dm_federation_destinations = await queue_dm_authority_mutation(
            session,
            settings,
            access,
            auth.user,
            "dm.message.update",
            {"message": result},
        )
    await session.commit()
    if automod_post_commit is not None:
        await automod_post_commit.publish(redis)
    if access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    for destination in dm_federation_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    if announcement_effects is not None:
        await announcement_effects.publish(redis)
    if access.guild is not None:
        await publish_channel_dispatch(redis, access, "MESSAGE_UPDATE", result)
    else:
        # Poll results contain viewer-specific ``me_voted`` state. A DM edit
        # therefore cannot safely fan one actor-rendered projection out to all
        # participants even though the stored edit itself is shared.
        for participant in access.participants:
            participant_result = (
                result
                if (participant.id, participant.origin_domain)
                == (auth.user.id, auth.user.origin_domain)
                else await render_message_payload(session, message, viewer=participant)
            )
            await publish_dispatch(
                redis,
                user_topic(participant.origin_domain, participant.id),
                "MESSAGE_UPDATE",
                participant_result,
            )
    for attachment in added_attachments:
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
    for attachment in removed_attachments:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
    return result


async def lock_message_delete_access(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
) -> ChannelAccess:
    """Acquire the canonical terminal-room and guild/channel mutation fences."""

    if access.guild is not None:
        if access.channel.type == 5:
            await lock_announcement_mutation(session)
        await lock_terminal_room(
            session,
            "guild",
            access.guild.id,
            access.guild.origin_domain,
        )
    return await lock_local_channel_mutation(session, settings, access)


async def lock_message_delete_target(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    message_id: EntityReferenceLike,
) -> Message:
    """Fence every message attachment tombstone before locking the message row."""

    channel = access.channel
    unlocked_message = await channel_message(
        session,
        settings,
        channel,
        message_id,
        for_update=False,
    )
    predelete_attachment_refs = set(
        (
            await session.execute(
                select(Attachment.id, Attachment.origin_domain).where(
                    Attachment.message_id == unlocked_message.id,
                    Attachment.message_domain == unlocked_message.origin_domain,
                )
            )
        ).tuples()
    )
    for attachment_id, attachment_domain in sorted(
        predelete_attachment_refs, key=lambda ref: (ref[1], ref[0])
    ):
        await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    return await channel_message(session, settings, channel, message_id, for_update=True)


async def commit_local_message_deletion(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    message: Message,
) -> bool:
    """Commit and publish one authoritative local message tombstone.

    Human, bot, and incoming-webhook delete routes share this convergence path
    so thread counters, attachment terminalization, federation mutations, and
    live dispatch cannot drift by authorization mechanism.
    """

    channel = access.channel
    already_deleted = message.deleted_at is not None
    announcement_effects: AnnouncementSyncEffects | None = None
    dm_federation_destinations: set[str] = set()
    if not already_deleted:
        message.content = None
        message.e2ee = None
        message.deleted_at = datetime.now(UTC)
        await session.execute(
            delete(Pin).where(
                Pin.message_id == message.id,
                Pin.message_domain == message.origin_domain,
            )
        )
        if channel.type in THREAD_CHANNEL_TYPES and (
            message.id,
            message.origin_domain,
        ) != (channel.starter_message_id, channel.starter_message_domain):
            channel.message_count = max(0, int(getattr(channel, "message_count", 0) or 0) - 1)
        if channel.type in THREAD_CHANNEL_TYPES and (
            channel.last_message_id,
            channel.last_message_domain,
        ) == (message.id, message.origin_domain):
            await refresh_thread_last_message_after_delete(session, channel)
        if access.guild is not None and int(getattr(message, "flags", 0) or 0) & (
            MESSAGE_FLAG_CROSSPOSTED
        ):
            announcement_effects = await propagate_announcement_source_change(
                session,
                settings,
                None,
                access.guild,
                message,
                actor,
                source_deleted=True,
                changed_at=message.deleted_at,
            )
        deleted_attachments, media_destinations = await queue_attachment_tombstones(
            session, settings, access, actor, [message]
        )
        if access.guild is not None:
            await queue_guild_mutation(
                session,
                settings,
                access.guild,
                actor,
                "guild.message.delete",
                {
                    "message": {
                        "id": str(message.id),
                        "origin_domain": message.origin_domain,
                    },
                    "deleted_at": message.deleted_at.isoformat(),
                },
                channel=channel,
            )
            if channel.type in THREAD_CHANNEL_TYPES:
                # Replicas apply the message delta first, then this complete
                # projection is an exact convergence fence.
                await queue_guild_mutation(
                    session,
                    settings,
                    access.guild,
                    actor,
                    "guild.channel.update",
                    {"channel": federation_channel_state(channel)},
                    channel=channel,
                )
        else:
            dm_federation_destinations = await queue_dm_authority_mutation(
                session,
                settings,
                access,
                actor,
                "dm.message.delete",
                {
                    "message_id": str(message.id),
                    "message_domain": message.origin_domain,
                    "channel_id": str(channel.id),
                    "channel_domain": channel.origin_domain,
                    "deleted_at": message.deleted_at.isoformat(),
                },
            )
    else:
        deleted_attachments, media_destinations = [], set()
    rendered_thread: dict[str, object] | None = None
    if not already_deleted and channel.type in THREAD_CHANNEL_TYPES:
        await materialize_updated_at(session, channel)
        rendered_thread = channel_payload(channel)
    await session.commit()
    if not already_deleted and access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    for destination in dm_federation_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    if announcement_effects is not None:
        await announcement_effects.publish(redis)
    if not already_deleted:
        for attachment in deleted_attachments:
            await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
        for destination in media_destinations:
            await enqueue_best_effort(federation_deliver, destination)
        await publish_channel_dispatch(
            redis,
            access,
            "MESSAGE_DELETE",
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
            },
        )
        if channel.type in THREAD_CHANNEL_TYPES:
            if rendered_thread is None:
                raise RuntimeError("thread projection was not materialized before commit")
            await publish_channel_dispatch(redis, access, "THREAD_UPDATE", rendered_thread)
    return not already_deleted


@router.delete("/{channel_id}/messages/{message_id}", status_code=204)
async def delete_message(
    channel_id: EntityRef,
    message_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        await proxy_remote_guild_message_operation(
            session,
            settings,
            access,
            auth.user,
            "message.delete",
            message_ref=message_id,
        )
        return Response(status_code=204)
    if access.guild is None and access.channel.origin_domain != settings.domain:
        await proxy_remote_dm_message_operation(
            session,
            settings,
            access,
            auth.user,
            "message.delete",
            message_id,
        )
        return Response(status_code=204)
    require_local_mutation_authority(access, settings)
    access = await lock_message_delete_access(session, settings, access)
    channel = access.channel
    actor_permissions = await require_channel_permissions(
        session, redis, access, auth.user, required_permissions("message.delete.self")
    )
    require_thread_message_delete_state(channel, actor_permissions)
    message = await lock_message_delete_target(session, settings, access, message_id)
    if (message.author_id, message.author_domain) != (auth.user.id, auth.user.origin_domain):
        if access.guild is None:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("message.delete.other")
        )
    await commit_local_message_deletion(
        session,
        redis,
        settings,
        access,
        auth.user,
        message,
    )
    return Response(status_code=204)


@router.post("/{channel_id}/messages/bulk-delete", status_code=204)
async def bulk_delete_messages(
    channel_id: EntityRef,
    payload: MessageBulkDelete,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        await proxy_remote_guild_message_operation(
            session,
            settings,
            access,
            auth.user,
            "message.bulk_delete",
            message_refs=payload.message_ids,
        )
        return Response(status_code=204)
    require_local_mutation_authority(access, settings)
    if access.guild is not None:
        if access.channel.type == 5:
            await lock_announcement_mutation(session)
        await lock_terminal_room(
            session,
            "guild",
            access.guild.id,
            access.guild.origin_domain,
        )
    access = await lock_local_channel_mutation(session, settings, access)
    if access.guild is None:
        raise HTTPException(status_code=400, detail={"code": "BULK_DELETE_NOT_SUPPORTED"})
    actor_permissions = await require_channel_permissions(
        session, redis, access, auth.user, required_permissions("message.bulk_delete")
    )
    require_thread_message_delete_state(access.channel, actor_permissions)
    message_refs = [item.resolve(settings.domain) for item in payload.message_ids]
    predelete_attachment_refs = set(
        (
            await session.execute(
                select(Attachment.id, Attachment.origin_domain)
                .join(
                    Message,
                    (Message.id == Attachment.message_id)
                    & (Message.origin_domain == Attachment.message_domain),
                )
                .where(
                    tuple_(Message.id, Message.origin_domain).in_(message_refs),
                    Message.channel_id == access.channel.id,
                    Message.channel_domain == access.channel.origin_domain,
                )
            )
        ).tuples()
    )
    for attachment_id, attachment_domain in sorted(
        predelete_attachment_refs, key=lambda ref: (ref[1], ref[0])
    ):
        await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    messages = list(
        await session.scalars(
            select(Message)
            .where(
                tuple_(Message.id, Message.origin_domain).in_(message_refs),
                Message.channel_id == access.channel.id,
                Message.channel_domain == access.channel.origin_domain,
            )
            .with_for_update()
        )
    )
    if len(messages) != len(message_refs):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    active_deleted_count = sum(
        message.deleted_at is None
        and (message.id, message.origin_domain)
        != (
            access.channel.starter_message_id,
            access.channel.starter_message_domain,
        )
        for message in messages
    )
    deleted_at = datetime.now(UTC)
    announcement_effects = AnnouncementSyncEffects()
    for deleted_message in messages:
        deleted_message.content = None
        deleted_message.e2ee = None
        deleted_message.deleted_at = deleted_at
        if int(getattr(deleted_message, "flags", 0) or 0) & MESSAGE_FLAG_CROSSPOSTED:
            announcement_effects.merge(
                await propagate_announcement_source_change(
                    session,
                    settings,
                    None,
                    access.guild,
                    deleted_message,
                    auth.user,
                    source_deleted=True,
                    changed_at=deleted_at,
                )
            )
    await session.execute(
        delete(Pin).where(
            tuple_(Pin.message_id, Pin.message_domain).in_(message_refs),
            Pin.channel_id == access.channel.id,
            Pin.channel_domain == access.channel.origin_domain,
        )
    )
    # The next-last-message query must never consider one of this batch.
    await session.flush()
    deleted_attachments, media_destinations = await queue_attachment_tombstones(
        session, settings, access, auth.user, messages
    )
    thread_projection_changed = False
    if access.channel.type in THREAD_CHANNEL_TYPES:
        if active_deleted_count:
            access.channel.message_count = max(
                0,
                int(getattr(access.channel, "message_count", 0) or 0) - active_deleted_count,
            )
            thread_projection_changed = True
        if (access.channel.last_message_id, access.channel.last_message_domain) in {
            (message.id, message.origin_domain) for message in messages
        }:
            await refresh_thread_last_message_after_delete(session, access.channel)
            thread_projection_changed = True
    await session.execute(
        update(Message)
        .where(
            tuple_(Message.id, Message.origin_domain).in_(message_refs),
            Message.channel_id == access.channel.id,
            Message.channel_domain == access.channel.origin_domain,
        )
        .values(content=None, e2ee=None, deleted_at=deleted_at)
    )
    await queue_guild_mutation(
        session,
        settings,
        access.guild,
        auth.user,
        "guild.message.bulk_delete",
        {
            "messages": [
                {
                    "id": str(deleted_message.id),
                    "origin_domain": deleted_message.origin_domain,
                }
                for deleted_message in messages
            ],
            "deleted_at": deleted_at.isoformat(),
        },
        channel=access.channel,
    )
    if thread_projection_changed:
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            auth.user,
            "guild.channel.update",
            {"channel": federation_channel_state(access.channel)},
            channel=access.channel,
        )
    rendered_thread: dict[str, object] | None = None
    if thread_projection_changed:
        await materialize_updated_at(session, access.channel)
        rendered_thread = channel_payload(access.channel)
    await session.commit()
    await wake_queued_guild_federation(access.guild)
    await announcement_effects.publish(redis)
    for attachment in deleted_attachments:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
    for destination in media_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    await publish_channel_dispatch(
        redis,
        access,
        "MESSAGE_DELETE_BULK",
        {
            "ids": [
                {"id": str(message.id), "origin_domain": message.origin_domain}
                for message in messages
            ],
            "channel_id": str(access.channel.id),
            "channel_domain": access.channel.origin_domain,
            "guild_id": str(access.guild.id),
            "guild_domain": access.guild.origin_domain,
        },
    )
    if thread_projection_changed:
        if rendered_thread is None:
            raise RuntimeError("thread projection was not materialized before commit")
        await publish_channel_dispatch(redis, access, "THREAD_UPDATE", rendered_thread)
    return Response(status_code=204)


def reaction_path_emoji(value: str) -> str:
    try:
        return canonical_reaction_emoji(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "REACTION_EMOJI_INVALID"}) from exc


@router.put("/{channel_id}/messages/{message_id}/reactions/{emoji}/@me", status_code=204)
async def add_own_reaction(
    channel_id: EntityRef,
    message_id: EntityRef,
    response: Response,
    emoji: str = Path(min_length=1, max_length=320),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Discord-compatible bodyless reaction route for ordinary expressions."""

    payload = ReactionCreate(emoji=reaction_path_emoji(emoji))
    return await add_reaction(
        channel_id,
        message_id,
        payload,
        response,
        auth,
        session,
        redis,
        settings,
    )


@router.post("/{channel_id}/messages/{message_id}/reactions", status_code=204)
async def add_reaction(
    channel_id: EntityRef,
    message_id: EntityRef,
    payload: ReactionCreate,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["reaction"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    access = await load_channel_access(session, settings, auth.user, channel_id)
    require_unarchived_thread(access.channel)
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        actor_permissions = await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("reaction.create")
        )
        (
            expression_authorizations,
            expression_application_ref,
        ) = await prepare_reaction_expression_authorizations(
            session,
            redis,
            settings,
            access,
            auth.user,
            message_id,
            payload.emoji,
            payload.expression_actor_intents,
            actor_permissions,
        )
        return await proxy_remote_guild_reaction(
            session,
            settings,
            access,
            auth.user,
            message_id,
            payload.emoji,
            remove=False,
            expression_authorizations=expression_authorizations,
            application_ref=expression_application_ref,
        )
    if access.guild is None and access.channel.origin_domain != settings.domain:
        await proxy_remote_dm_message_operation(
            session,
            settings,
            access,
            auth.user,
            "reaction.add",
            message_id,
            emoji=payload.emoji,
        )
        response.status_code = 204
        return response
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    actor_permissions = await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("reaction.create"),
    )
    if access.guild is not None:
        await require_member_interactions_allowed(
            session,
            access.guild,
            auth.user,
            Permission.ADD_REACTIONS,
        )
    await require_dm_send(session, access, auth.user)
    if access.guild is not None:
        await prepare_reaction_expression_authorizations(
            session,
            redis,
            settings,
            access,
            auth.user,
            message_id,
            payload.emoji,
            payload.expression_actor_intents,
            actor_permissions,
        )
    else:
        if payload.expression_actor_intents:
            raise HTTPException(
                status_code=400,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            )
        await validate_custom_emoji_use(
            session,
            auth.user,
            payload.emoji,
            target_guild=None,
            target_permissions=actor_permissions,
        )
    message = await channel_message(
        session,
        settings,
        channel,
        message_id,
        for_update=True,
        require_active=True,
    )
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
    if (
        access.guild is not None
        and not emoji_exists
        and not actor_permissions & Permission.ADD_REACTIONS
    ):
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    if access.guild is None:
        existing_reaction = await session.get(
            Reaction,
            (
                message.id,
                message.origin_domain,
                auth.user.id,
                auth.user.origin_domain,
                payload.emoji,
            ),
        )
        if existing_reaction is None:
            retained_reactions = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Reaction)
                    .where(
                        Reaction.message_id == message.id,
                        Reaction.message_domain == message.origin_domain,
                    )
                )
                or 0
            )
            if retained_reactions >= DM_REACTIONS_PER_MESSAGE_LIMIT:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "DM_REACTION_LIMIT_REACHED",
                        "limit": DM_REACTIONS_PER_MESSAGE_LIMIT,
                    },
                )
    inserted = await session.scalar(
        pg_insert(Reaction)
        .values(
            message_id=message.id,
            message_domain=message.origin_domain,
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
            emoji_key=payload.emoji,
        )
        .on_conflict_do_nothing()
        .returning(Reaction.message_id)
    )
    federation_destinations: set[str] = set()
    if inserted is not None:
        await mark_guild_activity(session, settings, access, auth.user)
        if access.guild is not None:
            await queue_guild_mutation(
                session,
                settings,
                access.guild,
                auth.user,
                "guild.reaction.add",
                {
                    "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                    "user": {"id": str(auth.user.id), "origin_domain": auth.user.origin_domain},
                    "emoji": payload.emoji,
                },
                channel=channel,
            )
        else:
            federation_destinations = await queue_dm_authority_mutation(
                session,
                settings,
                access,
                auth.user,
                "dm.reaction.add",
                dm_message_mutation_content(
                    access,
                    message,
                    auth.user,
                    emoji=payload.emoji,
                ),
            )
        await session.commit()
        if access.guild is not None:
            await wake_queued_guild_federation(access.guild)
        for destination in federation_destinations:
            await enqueue_best_effort(federation_deliver, destination)
        await publish_channel_dispatch(
            redis,
            access,
            "MESSAGE_REACTION_ADD",
            reaction_event_payload(
                message_id=message.id,
                message_domain=message.origin_domain,
                channel_id=access.channel.id,
                channel_domain=access.channel.origin_domain,
                user_id=auth.user.id,
                user_domain=auth.user.origin_domain,
                emoji=payload.emoji,
                guild_id=access.guild.id if access.guild is not None else None,
                guild_domain=access.guild.origin_domain if access.guild is not None else None,
                message_author_id=message.author_id,
                message_author_domain=message.author_domain,
            ),
        )
    else:
        await session.commit()
    response.status_code = 204
    return response


@router.get("/{channel_id}/messages/{message_id}/reactions/{emoji}")
async def list_reaction_users(
    channel_id: EntityRef,
    message_id: EntityRef,
    emoji: str = Path(min_length=1, max_length=320),
    after: EntityRef | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """List users for one reaction without expanding ordinary message payloads."""

    emoji = reaction_path_emoji(emoji)
    access = await load_channel_access(session, settings, auth.user, channel_id)
    await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("reaction.list"),
    )
    if access.guild is None and access.channel.origin_domain != settings.domain:
        return await proxy_remote_dm_message_operation(
            session,
            settings,
            access,
            auth.user,
            "reaction.list",
            message_id,
            emoji=emoji,
            after=after,
            limit=limit,
        )
    message = await channel_message(session, settings, access.channel, message_id)
    conditions = [
        Reaction.message_id == message.id,
        Reaction.message_domain == message.origin_domain,
        Reaction.emoji_key == emoji,
    ]
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
        conditions.append(tuple_(Reaction.user_id, Reaction.user_domain) > (after_id, after_domain))
    statement = (
        select(User)
        .join(
            Reaction,
            (Reaction.user_id == User.id) & (Reaction.user_domain == User.origin_domain),
        )
        .where(*conditions)
        .order_by(Reaction.user_id, Reaction.user_domain)
        .limit(limit + 1)
    )
    users = list(await session.scalars(statement))
    has_more = len(users) > limit
    page = users[:limit]
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(Reaction)
            .where(
                Reaction.message_id == message.id,
                Reaction.message_domain == message.origin_domain,
                Reaction.emoji_key == emoji,
            )
        )
        or 0
    )
    return {
        "items": [user_payload(user) for user in page],
        "total": total,
        "next_after": (f"{page[-1].id}@{page[-1].origin_domain}" if has_more and page else None),
    }


@router.delete("/{channel_id}/messages/{message_id}/reactions/{emoji}/@me", status_code=204)
async def remove_own_reaction(
    channel_id: EntityRef,
    message_id: EntityRef,
    response: Response,
    emoji: str = Path(min_length=1, max_length=320),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    emoji = reaction_path_emoji(emoji)
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["reaction"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    access = await load_channel_access(session, settings, auth.user, channel_id)
    require_unarchived_thread(access.channel)
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("reaction.delete.self")
        )
        return await proxy_remote_guild_reaction(
            session,
            settings,
            access,
            auth.user,
            message_id,
            emoji,
            remove=True,
        )
    if access.guild is None and access.channel.origin_domain != settings.domain:
        await proxy_remote_dm_message_operation(
            session,
            settings,
            access,
            auth.user,
            "reaction.remove",
            message_id,
            emoji=emoji,
        )
        response.status_code = 204
        return response
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    await require_channel_permissions(
        session, redis, access, auth.user, required_permissions("reaction.delete.self")
    )
    message = await channel_message(session, settings, access.channel, message_id)
    removed = await session.scalar(
        delete(Reaction)
        .where(
            Reaction.message_id == message.id,
            Reaction.message_domain == message.origin_domain,
            Reaction.user_id == auth.user.id,
            Reaction.user_domain == auth.user.origin_domain,
            Reaction.emoji_key == emoji,
        )
        .returning(Reaction.message_id)
    )
    federation_destinations: set[str] = set()
    if removed is not None and access.guild is not None:
        await mark_guild_activity(session, settings, access, auth.user)
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            auth.user,
            "guild.reaction.remove",
            {
                "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                "user": {"id": str(auth.user.id), "origin_domain": auth.user.origin_domain},
                "emoji": emoji,
            },
            channel=access.channel,
        )
    elif removed is not None:
        federation_destinations = await queue_dm_authority_mutation(
            session,
            settings,
            access,
            auth.user,
            "dm.reaction.remove",
            dm_message_mutation_content(
                access,
                message,
                auth.user,
                emoji=emoji,
            ),
        )
    await session.commit()
    if removed is not None and access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    for destination in federation_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    if removed is not None:
        await publish_channel_dispatch(
            redis,
            access,
            "MESSAGE_REACTION_REMOVE",
            reaction_event_payload(
                message_id=message.id,
                message_domain=message.origin_domain,
                channel_id=access.channel.id,
                channel_domain=access.channel.origin_domain,
                user_id=auth.user.id,
                user_domain=auth.user.origin_domain,
                emoji=emoji,
                guild_id=access.guild.id if access.guild is not None else None,
                guild_domain=access.guild.origin_domain if access.guild is not None else None,
                message_author_id=message.author_id,
                message_author_domain=message.author_domain,
                removed=True,
            ),
        )
    response.status_code = 204
    return response


@router.delete("/{channel_id}/messages/{message_id}/reactions/{emoji}/{user_id}", status_code=204)
async def remove_user_reaction(
    channel_id: EntityRef,
    message_id: EntityRef,
    user_id: EntityRef,
    emoji: str = Path(min_length=1, max_length=320),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    emoji = reaction_path_emoji(emoji)
    access = await load_channel_access(session, settings, auth.user, channel_id)
    require_unarchived_thread(access.channel)
    user_number, user_domain = user_id.resolve(settings.domain)
    if access.guild is None:
        if (user_number, user_domain) != (auth.user.id, auth.user.origin_domain):
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
        # Discord's target-user form may name the caller in a DM, but never
        # grants moderation over the other participant. Reuse the ordinary
        # self-removal path so remote authority routing and durable DM relay
        # cannot drift between equivalent endpoints.
        return await remove_own_reaction(
            channel_id,
            message_id,
            Response(),
            emoji,
            auth,
            session,
            redis,
            settings,
        )
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        await proxy_remote_guild_message_operation(
            session,
            settings,
            access,
            auth.user,
            "reaction.remove_user",
            message_ref=message_id,
            emoji=emoji,
            target_user_ref=user_id,
        )
        return Response(status_code=204)
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    if (user_number, user_domain) != (auth.user.id, auth.user.origin_domain):
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("reaction.delete.other")
        )
    else:
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("reaction.delete.self")
        )
    message = await channel_message(session, settings, access.channel, message_id)
    removed = await session.scalar(
        delete(Reaction)
        .where(
            Reaction.message_id == message.id,
            Reaction.message_domain == message.origin_domain,
            Reaction.user_id == user_number,
            Reaction.user_domain == user_domain,
            Reaction.emoji_key == emoji,
        )
        .returning(Reaction.message_id)
    )
    if removed is not None and access.guild is not None:
        await mark_guild_activity(session, settings, access, auth.user)
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            auth.user,
            "guild.reaction.remove",
            {
                "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                "user": {"id": str(user_number), "origin_domain": user_domain},
                "emoji": emoji,
            },
            channel=access.channel,
        )
    await session.commit()
    if removed is not None and access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    if removed is not None:
        await publish_channel_dispatch(
            redis,
            access,
            "MESSAGE_REACTION_REMOVE",
            reaction_event_payload(
                message_id=message.id,
                message_domain=message.origin_domain,
                channel_id=access.channel.id,
                channel_domain=access.channel.origin_domain,
                user_id=user_number,
                user_domain=user_domain,
                emoji=emoji,
                guild_id=access.guild.id if access.guild is not None else None,
                guild_domain=access.guild.origin_domain if access.guild is not None else None,
                message_author_id=message.author_id,
                message_author_domain=message.author_domain,
                removed=True,
            ),
        )
    return Response(status_code=204)


async def _clear_reactions(
    channel_id: EntityRef,
    message_id: EntityRef,
    emoji: str | None,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> Response:
    if emoji is not None:
        emoji = reaction_path_emoji(emoji)
    access = await load_channel_access(session, settings, auth.user, channel_id)
    require_unarchived_thread(access.channel)
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        await proxy_remote_guild_message_operation(
            session,
            settings,
            access,
            auth.user,
            "reaction.clear",
            message_ref=message_id,
            emoji=emoji,
        )
        return Response(status_code=204)
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    if access.guild is None:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("reaction.delete.other"),
    )
    message = await channel_message(session, settings, access.channel, message_id)
    conditions = [
        Reaction.message_id == message.id,
        Reaction.message_domain == message.origin_domain,
    ]
    if emoji is not None:
        conditions.append(Reaction.emoji_key == emoji)
    removed = list(
        (
            await session.execute(
                delete(Reaction)
                .where(*conditions)
                .returning(Reaction.user_id, Reaction.user_domain, Reaction.emoji_key)
            )
        ).tuples()
    )
    if removed:
        await mark_guild_activity(session, settings, access, auth.user)
        mutation_content: dict[str, object] = {
            "message": {"id": str(message.id), "origin_domain": message.origin_domain},
        }
        if emoji is not None:
            mutation_content["emoji"] = emoji
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            auth.user,
            "guild.reaction.clear",
            mutation_content,
            channel=access.channel,
        )
    await session.commit()
    if removed:
        await wake_queued_guild_federation(access.guild)
        event_payload: dict[str, object] = {
            "message_id": str(message.id),
            "message_domain": message.origin_domain,
            "channel_id": str(access.channel.id),
            "channel_domain": access.channel.origin_domain,
            "guild_id": str(access.guild.id),
            "guild_domain": access.guild.origin_domain,
        }
        event_type = "MESSAGE_REACTION_REMOVE_ALL"
        if emoji is not None:
            event_type = "MESSAGE_REACTION_REMOVE_EMOJI"
            event_payload["reaction"] = emoji
            event_payload["emoji"] = reaction_emoji_payload(emoji)
        await publish_channel_dispatch(redis, access, event_type, event_payload)
    return Response(status_code=204)


@router.delete("/{channel_id}/messages/{message_id}/reactions", status_code=204)
async def clear_all_reactions(
    channel_id: EntityRef,
    message_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Clear every reaction on a message."""

    return await _clear_reactions(channel_id, message_id, None, auth, session, redis, settings)


@router.delete(
    "/{channel_id}/messages/{message_id}/reaction-groups/{emoji}",
    status_code=204,
    include_in_schema=False,
)
@router.delete("/{channel_id}/messages/{message_id}/reactions/{emoji}", status_code=204)
async def clear_reaction_group(
    channel_id: EntityRef,
    message_id: EntityRef,
    emoji: str = Path(min_length=1, max_length=320),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    return await _clear_reactions(channel_id, message_id, emoji, auth, session, redis, settings)


async def _poll_for_mutation(
    channel_id: EntityRef,
    message_id: EntityRef,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> tuple[ChannelAccess, Message, Poll]:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("message.list"),
    )
    message = await channel_message(
        session, settings, access.channel, message_id, require_active=True
    )
    poll = await session.scalar(
        select(Poll)
        .where(
            Poll.message_id == message.id,
            Poll.message_domain == message.origin_domain,
        )
        .with_for_update()
    )
    if poll is None:
        raise HTTPException(status_code=404, detail={"code": "POLL_NOT_FOUND"})
    return access, message, poll


async def _publish_poll_vote(
    redis: Redis,
    access: ChannelAccess,
    message: Message,
    user: User,
    answer_id: int,
    *,
    added: bool,
) -> None:
    await publish_channel_dispatch(
        redis,
        access,
        "MESSAGE_POLL_VOTE_ADD" if added else "MESSAGE_POLL_VOTE_REMOVE",
        {
            "message_id": str(message.id),
            "message_domain": message.origin_domain,
            "channel_id": str(access.channel.id),
            "channel_domain": access.channel.origin_domain,
            "guild_id": str(access.guild.id) if access.guild is not None else None,
            "guild_domain": access.guild.origin_domain if access.guild is not None else None,
            "user_id": str(user.id),
            "user_domain": user.origin_domain,
            "answer_id": answer_id,
        },
    )


async def queue_dm_poll_mutation(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    event_type: Literal["dm.poll.vote.add", "dm.poll.vote.remove", "dm.poll.finalize"],
    message: Message,
    *,
    answer_id: int | None = None,
    finalized_at: datetime | None = None,
) -> set[str]:
    """Queue one authority-signed DM poll delta for every participant home."""

    content: dict[str, object] = {
        "message_id": str(message.id),
        "message_domain": message.origin_domain,
        "channel_id": str(access.channel.id),
        "channel_domain": access.channel.origin_domain,
    }
    if event_type == "dm.poll.finalize":
        if finalized_at is None or answer_id is not None:
            raise RuntimeError("DM poll finalization metadata is incomplete")
        content["finalized_at"] = finalized_at.isoformat()
    else:
        if answer_id is None or finalized_at is not None:
            raise RuntimeError("DM poll vote metadata is incomplete")
        content["answer_id"] = answer_id
    return await queue_dm_authority_mutation(
        session,
        settings,
        access,
        actor,
        event_type,
        content,
    )


async def queue_dm_authority_mutation(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    event_type: str,
    content: dict[str, object],
) -> set[str]:
    """Queue one exact authority mutation for every remote DM participant home."""

    if event_type not in DM_MESSAGE_MUTATION_EVENTS and not event_type.startswith("dm.poll."):
        raise RuntimeError("unsupported DM authority mutation")
    if access.guild is not None:
        raise RuntimeError("DM authority mutation received guild access")
    conversation = await session.get(
        DMConversation,
        (access.channel.id, access.channel.origin_domain),
    )
    if (
        conversation is None
        or conversation.authority_domain != settings.domain
        or conversation.origin_domain != settings.domain
    ):
        raise RuntimeError("DM authority mutation must be committed by its authority")
    envelope = await build_envelope(
        session,
        settings,
        event_type,
        actor,
        content,
        context={
            "conversation_id": str(conversation.id),
            "conversation_domain": conversation.origin_domain,
        },
        authority_attested_actor=actor.origin_domain != settings.domain,
    )
    destinations = {
        participant.origin_domain
        for participant in access.participants
        if participant.origin_domain != settings.domain
    }
    for destination in destinations:
        await queue_event(session, settings, destination, envelope)
    return destinations


def dm_message_mutation_content(
    access: ChannelAccess,
    message: Message,
    actor: User,
    **extra: object,
) -> dict[str, object]:
    """Build the common composite identities for a durable DM mutation."""

    return {
        "message_id": str(message.id),
        "message_domain": message.origin_domain,
        "channel_id": str(access.channel.id),
        "channel_domain": access.channel.origin_domain,
        "user_id": str(actor.id),
        "user_domain": actor.origin_domain,
        **extra,
    }


@router.put(
    "/{channel_id}/messages/{message_id}/polls/answers/{answer_id}/@me",
    status_code=204,
)
async def add_poll_vote(
    channel_id: EntityRef,
    message_id: EntityRef,
    answer_id: int = Path(ge=1, le=10),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    initial_access = await load_channel_access(session, settings, auth.user, channel_id)
    if initial_access.guild is not None and initial_access.guild.origin_domain != settings.domain:
        await require_channel_permissions(
            session,
            redis,
            initial_access,
            auth.user,
            required_permissions("message.list"),
        )
        return await proxy_remote_guild_poll_vote(
            session,
            settings,
            initial_access,
            auth.user,
            message_id,
            answer_id,
            remove=False,
        )
    if initial_access.guild is None and initial_access.channel.origin_domain != settings.domain:
        await proxy_remote_dm_message_operation(
            session,
            settings,
            initial_access,
            auth.user,
            "poll.vote.add",
            message_id,
            answer_id=answer_id,
        )
        return Response(status_code=204)
    access, message, poll = await _poll_for_mutation(
        channel_id, message_id, auth, session, redis, settings
    )
    if access.guild is not None:
        await require_member_interactions_allowed(
            session,
            access.guild,
            auth.user,
            Permission.SEND_POLLS,
        )
    now = datetime.now(UTC)
    if poll.finalized_at is not None or poll.expires_at <= now:
        raise HTTPException(status_code=409, detail={"code": "POLL_FINALIZED"})
    answer = await session.get(PollAnswer, (message.id, message.origin_domain, answer_id))
    if answer is None:
        raise HTTPException(status_code=404, detail={"code": "POLL_ANSWER_NOT_FOUND"})
    removed_answers: list[int] = []
    if not poll.allow_multiselect:
        removed_answers = list(
            await session.scalars(
                delete(PollVote)
                .where(
                    PollVote.message_id == message.id,
                    PollVote.message_domain == message.origin_domain,
                    PollVote.user_id == auth.user.id,
                    PollVote.user_domain == auth.user.origin_domain,
                    PollVote.answer_id != answer_id,
                )
                .returning(PollVote.answer_id)
            )
        )
    inserted = await session.scalar(
        pg_insert(PollVote)
        .values(
            message_id=message.id,
            message_domain=message.origin_domain,
            answer_id=answer_id,
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
        )
        .on_conflict_do_nothing()
        .returning(PollVote.answer_id)
    )
    federation_destinations: set[str] = set()
    if inserted is not None or removed_answers:
        await mark_guild_activity(session, settings, access, auth.user)
        if access.guild is not None:
            for removed_answer in removed_answers:
                await queue_guild_mutation(
                    session,
                    settings,
                    access.guild,
                    auth.user,
                    "guild.poll.vote.remove",
                    {
                        "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                        "user": {"id": str(auth.user.id), "origin_domain": auth.user.origin_domain},
                        "answer_id": removed_answer,
                    },
                    channel=access.channel,
                )
            if inserted is not None:
                await queue_guild_mutation(
                    session,
                    settings,
                    access.guild,
                    auth.user,
                    "guild.poll.vote.add",
                    {
                        "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                        "user": {"id": str(auth.user.id), "origin_domain": auth.user.origin_domain},
                        "answer_id": answer_id,
                    },
                    channel=access.channel,
                )
        else:
            federation_destinations = await queue_dm_poll_mutation(
                session,
                settings,
                access,
                auth.user,
                "dm.poll.vote.add",
                message,
                answer_id=answer_id,
            )
    await session.commit()
    if access.guild is not None and (inserted is not None or removed_answers):
        await wake_queued_guild_federation(access.guild)
    for destination in federation_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    for removed_answer in removed_answers:
        await _publish_poll_vote(redis, access, message, auth.user, removed_answer, added=False)
    if inserted is not None:
        await _publish_poll_vote(redis, access, message, auth.user, answer_id, added=True)
    return Response(status_code=204)


@router.delete(
    "/{channel_id}/messages/{message_id}/polls/answers/{answer_id}/@me",
    status_code=204,
)
async def remove_poll_vote(
    channel_id: EntityRef,
    message_id: EntityRef,
    answer_id: int = Path(ge=1, le=10),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    initial_access = await load_channel_access(session, settings, auth.user, channel_id)
    if initial_access.guild is not None and initial_access.guild.origin_domain != settings.domain:
        await require_channel_permissions(
            session,
            redis,
            initial_access,
            auth.user,
            required_permissions("message.list"),
        )
        return await proxy_remote_guild_poll_vote(
            session,
            settings,
            initial_access,
            auth.user,
            message_id,
            answer_id,
            remove=True,
        )
    if initial_access.guild is None and initial_access.channel.origin_domain != settings.domain:
        await proxy_remote_dm_message_operation(
            session,
            settings,
            initial_access,
            auth.user,
            "poll.vote.remove",
            message_id,
            answer_id=answer_id,
        )
        return Response(status_code=204)
    access, message, poll = await _poll_for_mutation(
        channel_id, message_id, auth, session, redis, settings
    )
    if poll.finalized_at is not None or poll.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=409, detail={"code": "POLL_FINALIZED"})
    removed = await session.scalar(
        delete(PollVote)
        .where(
            PollVote.message_id == message.id,
            PollVote.message_domain == message.origin_domain,
            PollVote.answer_id == answer_id,
            PollVote.user_id == auth.user.id,
            PollVote.user_domain == auth.user.origin_domain,
        )
        .returning(PollVote.answer_id)
    )
    federation_destinations: set[str] = set()
    if removed is not None:
        await mark_guild_activity(session, settings, access, auth.user)
        if access.guild is not None:
            await queue_guild_mutation(
                session,
                settings,
                access.guild,
                auth.user,
                "guild.poll.vote.remove",
                {
                    "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                    "user": {"id": str(auth.user.id), "origin_domain": auth.user.origin_domain},
                    "answer_id": answer_id,
                },
                channel=access.channel,
            )
        else:
            federation_destinations = await queue_dm_poll_mutation(
                session,
                settings,
                access,
                auth.user,
                "dm.poll.vote.remove",
                message,
                answer_id=answer_id,
            )
    await session.commit()
    if access.guild is not None and removed is not None:
        await wake_queued_guild_federation(access.guild)
    for destination in federation_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    if removed is not None:
        await _publish_poll_vote(redis, access, message, auth.user, answer_id, added=False)
    return Response(status_code=204)


@router.get(
    "/{channel_id}/messages/{message_id}/polls/answers/{answer_id}",
)
async def list_poll_voters(
    channel_id: EntityRef,
    message_id: EntityRef,
    answer_id: int = Path(ge=1, le=10),
    after: EntityRef | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    await require_channel_permissions(
        session, redis, access, auth.user, required_permissions("message.list")
    )
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        return await proxy_remote_guild_poll_voters(
            session,
            settings,
            access,
            auth.user,
            message_id,
            answer_id,
            after=after,
            limit=limit,
        )
    if access.guild is None and access.channel.origin_domain != settings.domain:
        return await proxy_remote_dm_message_operation(
            session,
            settings,
            access,
            auth.user,
            "poll.voters.list",
            message_id,
            answer_id=answer_id,
            after=after,
            limit=limit,
        )
    message = await channel_message(session, settings, access.channel, message_id)
    conditions = [
        PollVote.message_id == message.id,
        PollVote.message_domain == message.origin_domain,
        PollVote.answer_id == answer_id,
    ]
    if after is not None:
        conditions.append(
            tuple_(PollVote.user_id, PollVote.user_domain) > after.resolve(settings.domain)
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
            .limit(limit + 1)
        )
    )
    has_more = len(users) > limit
    page = users[:limit]
    return {
        "users": [user_payload(user) for user in page],
        "next_after": (f"{page[-1].id}@{page[-1].origin_domain}" if has_more and page else None),
    }


async def ensure_poll_result_message(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    source: Message,
    poll: Poll,
) -> tuple[dict[str, object], bool]:
    """Create the automatic type-46 result exactly once at channel authority."""

    locked_poll = await session.scalar(
        select(Poll)
        .where(
            Poll.message_id == source.id,
            Poll.message_domain == source.origin_domain,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_poll is None:
        raise RuntimeError("poll result source disappeared during finalization")
    poll = locked_poll
    existing = await session.scalar(
        select(Message)
        .where(
            Message.channel_id == source.channel_id,
            Message.channel_domain == source.channel_domain,
            Message.message_type == POLL_RESULT_MESSAGE_TYPE,
            Message.referenced_message_id == source.id,
            Message.referenced_message_domain == source.origin_domain,
        )
        .order_by(Message.id, Message.origin_domain)
        .limit(1)
    )
    if existing is not None:
        return await render_message_payload(session, existing), False
    if poll.finalized_at is None:
        raise RuntimeError("poll result cannot precede poll finalization")
    author = await session.get(User, (source.author_id, source.author_domain))
    if author is None:
        raise RuntimeError("poll author disappeared before result materialization")
    projection, embed = await poll_result_material(session, source, poll)
    nonce = (
        "poll-result-v1-"
        + hashlib.sha256(f"{source.id}@{source.origin_domain}".encode()).hexdigest()[:40]
    )
    payload = MessageCreate.model_validate(
        {
            "embeds": [{str(key): value for key, value in embed.items() if key != "type"}],
            "client_nonce": nonce,
            "referenced_message_id": f"{source.id}@{source.origin_domain}",
            "mention_user_ids": [f"{author.id}@{author.origin_domain}"],
        }
    )
    rendered = await create_message(
        EntityRef(f"{source.channel_id}@{source.channel_domain}"),
        payload,
        Response(),
        federated_authenticated_user(author),
        session,
        redis,
        snowflake,
        settings,
        MessageAdmissionOptions(poll_result=projection, defer_dispatch=True),
    )
    return rendered, True


@router.post("/{channel_id}/messages/{message_id}/polls/expire")
async def finalize_poll(
    channel_id: EntityRef,
    message_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    initial_access = await load_channel_access(session, settings, auth.user, channel_id)
    if initial_access.guild is not None and initial_access.guild.origin_domain != settings.domain:
        await require_channel_permissions(
            session,
            redis,
            initial_access,
            auth.user,
            required_permissions("message.list"),
        )
        return await proxy_remote_guild_poll_finalize(
            session,
            settings,
            initial_access,
            auth.user,
            message_id,
        )
    if initial_access.guild is None and initial_access.channel.origin_domain != settings.domain:
        result = await proxy_remote_dm_message_operation(
            session,
            settings,
            initial_access,
            auth.user,
            "poll.end",
            message_id,
        )
        rendered = result.get("message")
        if not isinstance(rendered, dict):
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
            )
        return {str(key): value for key, value in rendered.items()}
    access, message, poll = await _poll_for_mutation(
        channel_id, message_id, auth, session, redis, settings
    )
    if (message.author_id, message.author_domain) != (auth.user.id, auth.user.origin_domain):
        raise HTTPException(status_code=403, detail={"code": "POLL_AUTHOR_REQUIRED"})
    changed = poll.finalized_at is None
    if changed and access.guild is not None:
        await require_member_interactions_allowed(
            session,
            access.guild,
            auth.user,
            Permission.SEND_POLLS,
        )
    result_message: dict[str, object] | None = None
    result_created = False
    if changed:
        poll.finalized_at = datetime.now(UTC)
        await session.flush()
        if access.guild is not None:
            await queue_guild_mutation(
                session,
                settings,
                access.guild,
                auth.user,
                "guild.poll.finalize",
                {
                    "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                    "finalized_at": poll.finalized_at.isoformat(),
                },
                channel=access.channel,
            )
        else:
            await queue_dm_poll_mutation(
                session,
                settings,
                access,
                auth.user,
                "dm.poll.finalize",
                message,
                finalized_at=poll.finalized_at,
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
    rendered = await render_message_payload(session, message, viewer=auth.user)
    if changed:
        await publish_channel_dispatch(redis, access, "MESSAGE_UPDATE", rendered)
        if result_created and result_message is not None:
            await publish_channel_dispatch(redis, access, "MESSAGE_CREATE", result_message)
    return rendered


def channel_follow_payload(
    follow: ChannelFollow | FederatedChannelFollow,
) -> dict[str, object]:
    federated = isinstance(follow, FederatedChannelFollow)
    authority_domain = (
        cast(FederatedChannelFollow, follow).target_authority_domain
        if federated
        else follow.target_channel_domain
    )
    return {
        "id": str(follow.id),
        "ref": qualified_follow_ref(follow.id, authority_domain),
        "source_channel_id": str(follow.source_channel_id),
        "source_channel_domain": follow.source_channel_domain,
        "target_channel_id": str(follow.target_channel_id),
        "target_channel_domain": follow.target_channel_domain,
        "creator_id": str(follow.creator_id),
        "creator_domain": follow.creator_domain,
        "active": follow.active,
        "federated": federated,
        "generation": (str(cast(FederatedChannelFollow, follow).generation) if federated else None),
        "lifecycle_state": (
            cast(FederatedChannelFollow, follow).lifecycle_state if federated else "active"
        ),
        "name": getattr(follow, "name", None),
        "avatar_hash": getattr(follow, "avatar_hash", None),
        "created_at": follow.created_at.isoformat(),
        "updated_at": follow.updated_at.isoformat(),
    }


CHANNEL_FOLLOW_PAYLOAD_FIELDS = frozenset(
    {
        "id",
        "ref",
        "source_channel_id",
        "source_channel_domain",
        "target_channel_id",
        "target_channel_domain",
        "creator_id",
        "creator_domain",
        "active",
        "federated",
        "generation",
        "lifecycle_state",
        "name",
        "avatar_hash",
        "created_at",
        "updated_at",
    }
)


def validate_channel_follow_response(
    raw: object,
    *,
    source_ref: tuple[int, str],
    target_ref: tuple[int, str] | None = None,
    creator_ref: tuple[int, str] | None = None,
    require_active: bool | None = None,
) -> dict[str, object]:
    """Validate one authority-rendered follower without trusting its routing fields."""

    if not isinstance(raw, dict) or set(raw) != CHANNEL_FOLLOW_PAYLOAD_FIELDS:
        raise ValueError("announcement follow response has an invalid shape")
    follow_id = validate_snowflake(raw.get("id"))
    parsed_source = (
        validate_snowflake(raw.get("source_channel_id")),
        normalize_domain(str(raw.get("source_channel_domain", ""))),
    )
    parsed_target = (
        validate_snowflake(raw.get("target_channel_id")),
        normalize_domain(str(raw.get("target_channel_domain", ""))),
    )
    parsed_creator = (
        validate_snowflake(raw.get("creator_id")),
        normalize_domain(str(raw.get("creator_domain", ""))),
    )
    if (
        raw.get("id") != str(follow_id)
        or raw.get("ref") != qualified_follow_ref(follow_id, parsed_target[1])
        or raw.get("source_channel_id") != str(parsed_source[0])
        or raw.get("source_channel_domain") != parsed_source[1]
        or raw.get("target_channel_id") != str(parsed_target[0])
        or raw.get("target_channel_domain") != parsed_target[1]
        or raw.get("creator_id") != str(parsed_creator[0])
        or raw.get("creator_domain") != parsed_creator[1]
        or parsed_source != source_ref
        or (target_ref is not None and parsed_target != target_ref)
        or (creator_ref is not None and parsed_creator != creator_ref)
    ):
        raise ValueError("announcement follow response escaped its request binding")
    active = raw.get("active")
    federated = raw.get("federated")
    lifecycle = raw.get("lifecycle_state")
    generation = raw.get("generation")
    if (
        type(active) is not bool
        or type(federated) is not bool
        or lifecycle not in {"pending", "accepted", "active", "revoked"}
        or active is not (lifecycle == "active")
        or (require_active is not None and active is not require_active)
    ):
        raise ValueError("announcement follow response has an invalid lifecycle")
    if federated:
        if (
            not isinstance(generation, str)
            or str(validate_snowflake(generation)) != generation
            or generation == "0"
        ):
            raise ValueError("announcement follow response has an invalid generation")
    elif generation is not None or lifecycle != "active":
        raise ValueError("local announcement follow response has federation state")
    for field_name, maximum in (("name", 80), ("avatar_hash", 128)):
        value = raw.get(field_name)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValueError("announcement follow response has invalid metadata")
    try:
        created_at = datetime.fromisoformat(cast(str, raw.get("created_at")))
        updated_at = datetime.fromisoformat(cast(str, raw.get("updated_at")))
    except (TypeError, ValueError):
        raise ValueError("announcement follow response has invalid timestamps") from None
    if created_at.tzinfo is None or updated_at.tzinfo is None or updated_at < created_at:
        raise ValueError("announcement follow response has invalid timestamps")
    return cast(dict[str, object], raw)


def validate_channel_follow_page(
    raw: object,
    *,
    source_ref: tuple[int, str],
) -> list[dict[str, object]]:
    """Validate the bounded, unique ordering of a follower collection."""

    if not isinstance(raw, list) or len(raw) > 10_000:
        raise ValueError("announcement follow page has an invalid shape")
    result: list[dict[str, object]] = []
    previous_ref: tuple[int, str] | None = None
    for item in raw:
        validated = validate_channel_follow_response(
            item,
            source_ref=source_ref,
            require_active=True,
        )
        follow_ref = (
            validate_snowflake(validated["id"]),
            EntityRef(str(validated["ref"])).resolve("invalid.local")[1],
        )
        if previous_ref is not None and follow_ref <= previous_ref:
            raise ValueError("announcement follow page has duplicate or unordered entries")
        previous_ref = follow_ref
        result.append(validated)
    return result


async def publish_follower_webhook_update(
    redis: Redis,
    guild: Guild,
    channel: Channel,
) -> None:
    """Publish Discord's destination-channel webhook collection invalidation."""

    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "WEBHOOKS_UPDATE",
        {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "channel_id": str(channel.id),
            "channel_domain": channel.origin_domain,
        },
    )


async def detach_announcement_follower_avatar(
    session: AsyncSession,
    follow: ChannelFollow | FederatedChannelFollow,
) -> Attachment | None:
    attachment = await session.scalar(
        select(Attachment)
        .where(
            Attachment.asset_binding
            == f"follower:{follow.target_channel_domain}:{follow.id}:avatar"
        )
        .with_for_update()
    )
    follow.avatar_hash = None
    if attachment is not None:
        attachment.asset_binding = None
    return attachment


def announcement_actor_application(auth: AuthenticatedUser) -> BotApplication | None:
    """Return the bot application carried by an internal bot auth context.

    Human HTTP authentication has no application. Bot API authentication uses
    ``BotPrincipal`` through the existing ``AuthenticatedUser`` adapter, and a
    federation authority creates the same narrow internal context after it has
    validated the signed public application reference. Tokens are never part
    of this value or any federation payload.
    """

    application = getattr(auth, "application", None)
    if application is None:
        return None
    if not isinstance(application, BotApplication):
        raise HTTPException(status_code=403, detail={"code": "BOT_INSTALLATION_REQUIRED"})
    return application


async def require_announcement_actor_scope(
    session: AsyncSession,
    access: ChannelAccess,
    actor: User,
    application: BotApplication | None,
    scope: str,
) -> None:
    """Recheck a bot's authoritative installation at the channel's guild.

    An instance signature authenticates the sending server, not an individual
    worker token. The bot home attests only the public application identity;
    every guild authority independently binds it to the exact bot user and an
    active installation with current membership and the required grant.
    """

    if application is None:
        if actor.account_type != "human":
            raise HTTPException(status_code=403, detail={"code": "BOT_INSTALLATION_REQUIRED"})
        return
    guild = access.guild
    if (
        actor.account_type != "bot"
        or application.status != "active"
        or (application.bot_user_id, application.bot_user_domain) != (actor.id, actor.origin_domain)
        or guild is None
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == application.id,
            BotInstallation.application_domain == application.origin_domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.bot_user_id == actor.id,
            BotInstallation.bot_user_domain == actor.origin_domain,
            usable_guild_installation(),
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    if scope not in installation.granted_scopes and not (
        scope == "webhooks.read" and "webhooks.manage" in installation.granted_scopes
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
        )


async def announcement_application_for_actor(
    session: AsyncSession,
    actor: User,
) -> BotApplication | None:
    """Resolve the one active application represented by an announcement actor."""

    if actor.account_type == "human":
        return None
    if actor.account_type != "bot":
        raise HTTPException(status_code=403, detail={"code": "USER_NOT_FOUND"})
    application = await session.scalar(
        select(BotApplication).where(
            BotApplication.bot_user_id == actor.id,
            BotApplication.bot_user_domain == actor.origin_domain,
            BotApplication.status == "active",
        )
    )
    if application is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    return application


@dataclass(frozen=True, slots=True)
class AnnouncementFollowSourceProjection:
    source_ref: tuple[int, str]
    source_guild_ref: tuple[int, str]
    source_channel_name: str
    target_ref: tuple[int, str]
    creator_ref: tuple[int, str]
    generation: int = 1
    authorization_id: str | None = None
    authorization_expires_at: datetime | None = None


def _announcement_source_authorization_content(
    content: dict[str, Any],
) -> AnnouncementFollowSourceProjection:
    if set(content) != {
        "source_channel_ref",
        "source_guild_ref",
        "source_channel_name",
        "target_channel_ref",
        "creator_ref",
        "generation",
        "authorization_id",
        "authorization_expires_at",
    }:
        raise ValueError("announcement source authorization is malformed")
    try:
        source_ref = EntityRef(str(content["source_channel_ref"])).resolve("invalid.local")
        source_guild_ref = EntityRef(str(content["source_guild_ref"])).resolve("invalid.local")
        target_ref = EntityRef(str(content["target_channel_ref"])).resolve("invalid.local")
        creator_ref = EntityRef(str(content["creator_ref"])).resolve("invalid.local")
        source_channel_name = content["source_channel_name"]
        raw_generation = content["generation"]
        if (
            not isinstance(raw_generation, str)
            or not raw_generation.isascii()
            or not raw_generation.isdecimal()
            or (len(raw_generation) > 1 and raw_generation.startswith("0"))
        ):
            raise ValueError
        generation = int(raw_generation)
        authorization_id = str(content["authorization_id"])
        authorization_expires_at = datetime.fromisoformat(str(content["authorization_expires_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("announcement source authorization is malformed") from exc
    if (
        source_guild_ref[1] != source_ref[1]
        or not isinstance(source_channel_name, str)
        or not 1 <= len(source_channel_name) <= 100
        or source_channel_name != source_channel_name.strip()
        or not 1 <= generation <= MAX_SNOWFLAKE
        or not authorization_id.startswith("kafi_")
        or len(authorization_id) != 48
        or authorization_expires_at.tzinfo is None
    ):
        raise ValueError("announcement source authorization metadata is invalid")
    return AnnouncementFollowSourceProjection(
        source_ref=source_ref,
        source_guild_ref=source_guild_ref,
        source_channel_name=source_channel_name,
        target_ref=target_ref,
        creator_ref=creator_ref,
        generation=generation,
        authorization_id=authorization_id,
        authorization_expires_at=authorization_expires_at,
    )


def announcement_follow_authorization_id(
    source_ref: tuple[int, str],
    target_ref: tuple[int, str],
    creator_ref: tuple[int, str],
    generation: int,
) -> str:
    """Return the stable correlation id for one follow generation.

    A caller may lose either synchronous federation response and repeat the
    whole prepare.  Keeping this id stable lets both channel authorities
    recognize that repeat without weakening the signed generation fence.
    """

    material = "\n".join(
        (
            "kaede-announcement-follow-v1",
            f"{source_ref[0]}@{source_ref[1]}",
            f"{target_ref[0]}@{target_ref[1]}",
            f"{creator_ref[0]}@{creator_ref[1]}",
            str(generation),
        )
    )
    return f"kafi_{hashlib.sha256(material.encode()).hexdigest()[:43]}"


async def authorize_federated_announcement_follow_source(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    application: BotApplication | None,
    source_channel_ref: EntityRef,
    target_channel_ref: EntityRef,
) -> dict[str, Any]:
    """Attest source metadata and live access before another authority follows it."""

    source = await load_channel_access(session, settings, actor, source_channel_ref)
    target_ref = target_channel_ref.resolve(settings.domain)
    if (
        source.guild is None
        or source.guild.origin_domain != settings.domain
        or source.channel.origin_domain != settings.domain
        or source.channel.type != 5
        or source.channel.encryption_mode == "e2ee"
        or source.channel.name is None
        or not source.channel.name.strip()
        or source.channel.name != source.channel.name.strip()
        or target_ref[1] == settings.domain
    ):
        raise HTTPException(status_code=400, detail={"code": "INVALID_ANNOUNCEMENT_FOLLOW"})
    await require_announcement_actor_scope(
        session,
        source,
        actor,
        application,
        "channels.read",
    )
    await require_channel_permissions(
        session,
        redis,
        source,
        actor,
        required_permissions("announcement.follow.source"),
    )
    await lock_announcement_mutation(session)
    await session.get(
        Guild,
        (source.guild.id, source.guild.origin_domain),
        with_for_update=True,
    )
    await session.refresh(source.channel)
    if (
        source.channel.type != 5
        or source.channel.encryption_mode == "e2ee"
        or source.channel.name is None
        or not source.channel.name.strip()
        or source.channel.name != source.channel.name.strip()
    ):
        raise HTTPException(status_code=400, detail={"code": "INVALID_ANNOUNCEMENT_FOLLOW"})
    await require_announcement_actor_scope(
        session,
        source,
        actor,
        application,
        "channels.read",
    )
    await require_channel_permissions(
        session,
        redis,
        source,
        actor,
        required_permissions("announcement.follow.source"),
    )
    existing = await session.scalar(
        select(FederatedChannelFollow)
        .where(
            FederatedChannelFollow.source_channel_id == source.channel.id,
            FederatedChannelFollow.source_channel_domain == source.channel.origin_domain,
            FederatedChannelFollow.target_channel_id == target_ref[0],
            FederatedChannelFollow.target_channel_domain == target_ref[1],
            FederatedChannelFollow.local_role == "source",
        )
        .with_for_update()
    )
    generation = (
        existing.generation + 1
        if existing is not None and existing.lifecycle_state == "revoked"
        else existing.generation
        if existing is not None
        else 1
    )
    signer = await guild_authority_owner(session, settings, source.guild)
    authorization_id = (
        existing.authorization_id
        if existing is not None
        and existing.lifecycle_state != "revoked"
        and existing.authorization_id is not None
        and (existing.creator_id, existing.creator_domain) == (actor.id, actor.origin_domain)
        else announcement_follow_authorization_id(
            (source.channel.id, source.channel.origin_domain),
            target_ref,
            (actor.id, actor.origin_domain),
            generation,
        )
    )
    authorization_expires_at = datetime.now(UTC) + ANNOUNCEMENT_FOLLOW_AUTHORIZATION_TTL
    authorization = await build_guild_authority_envelope(
        session,
        settings,
        source.guild,
        "guild.announcement.follow.source_authorized",
        signer,
        {
            "source_channel_ref": f"{source.channel.id}@{source.channel.origin_domain}",
            "source_guild_ref": f"{source.guild.id}@{source.guild.origin_domain}",
            "source_channel_name": source.channel.name,
            "target_channel_ref": f"{target_ref[0]}@{target_ref[1]}",
            "creator_ref": f"{actor.id}@{actor.origin_domain}",
            "generation": str(generation),
            "authorization_id": authorization_id,
            "authorization_expires_at": authorization_expires_at.isoformat(),
        },
        context={
            "guild_id": str(source.guild.id),
            "guild_domain": source.guild.origin_domain,
            "channel_id": str(source.channel.id),
            "channel_domain": source.channel.origin_domain,
        },
    )
    # Release the source-owner read/lock before a user home calls the target
    # authority. The signed envelope remains the immutable hand-off.
    await session.commit()
    return authorization


async def validated_federated_announcement_follow_source_authorization(
    session: AsyncSession,
    settings: Settings,
    actor: User | tuple[int, str],
    source_ref: tuple[int, str],
    target_ref: tuple[int, str],
    raw: object,
) -> AnnouncementFollowSourceProjection:
    """Fail closed on source metadata signed by the source guild authority."""

    try:
        if not isinstance(raw, dict):
            raise ValueError("source authorization is not an envelope")
        envelope = await validated_event_envelope(
            session,
            settings,
            source_ref[1],
            raw,
            allow_authority_attested_actor=True,
        )
        if envelope.type != "guild.announcement.follow.source_authorized":
            raise ValueError("source authorization has the wrong type")
        projection = _announcement_source_authorization_content(envelope.content)
        context = envelope.context
        actor_ref = (actor.id, actor.origin_domain) if isinstance(actor, User) else actor
        if (
            projection.source_ref != source_ref
            or projection.target_ref != target_ref
            or projection.creator_ref != actor_ref
            or set(context) != {"guild_id", "guild_domain", "channel_id", "channel_domain"}
            or context.get("guild_id") != str(projection.source_guild_ref[0])
            or context.get("guild_domain") != projection.source_guild_ref[1]
            or context.get("channel_id") != str(source_ref[0])
            or context.get("channel_domain") != source_ref[1]
            or projection.source_guild_ref[1] != source_ref[1]
            or projection.authorization_expires_at is None
            or projection.authorization_expires_at <= datetime.now(UTC)
        ):
            raise ValueError("source authorization binding is invalid")
    except (FederationNetworkError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "ANNOUNCEMENT_SOURCE_AUTHORIZATION_INVALID"},
        ) from exc
    return projection


async def request_federated_announcement_follow_source_authorization(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    application: BotApplication | None,
    source_ref: tuple[int, str],
    target_ref: tuple[int, str],
    actor_intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the source authority directly from the actor's authenticated home."""

    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            source_ref[1],
            f"/_kaede/v1/channels/{source_ref[0]}/announcement-follow-source-authorize",
            payload={
                "actor": profile_from_user(actor),
                "actor_application_ref": (
                    f"{application.id}@{application.origin_domain}"
                    if application is not None
                    else None
                ),
                "actor_intent": actor_intent,
                "target_channel_ref": f"{target_ref[0]}@{target_ref[1]}",
            },
            request_timeout=10,
            max_response_bytes=64 * 1024,
        )
    except FederationNetworkError:
        raise HTTPException(
            status_code=503,
            detail={"code": "ANNOUNCEMENT_SOURCE_AUTHORITY_UNAVAILABLE"},
        ) from None
    if response.status_code in {400, 403, 404, 409, 429}:
        raise_proxy_rejection(response, {400, 403, 404, 409, 429})
    if response.status_code != 200:
        raise HTTPException(
            status_code=503,
            detail={"code": "ANNOUNCEMENT_SOURCE_AUTHORITY_UNAVAILABLE"},
        )
    try:
        raw = decode_federation_response_json(response, max_response_bytes=64 * 1024)
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "ANNOUNCEMENT_SOURCE_AUTHORIZATION_INVALID"},
        ) from exc
    await validated_federated_announcement_follow_source_authorization(
        session,
        settings,
        actor,
        source_ref,
        target_ref,
        raw,
    )
    return cast(dict[str, Any], raw)


@dataclass(frozen=True, slots=True)
class AnnouncementFollowReceiptProjection:
    follow_id: int
    generation: int
    source_ref: tuple[int, str]
    source_guild_ref: tuple[int, str]
    source_channel_name: str
    target_ref: tuple[int, str]
    creator_ref: tuple[int, str]
    authorization_id: str
    authorization_expires_at: datetime


def _announcement_receipt_content(
    envelope_type: str,
    content: dict[str, Any],
) -> AnnouncementFollowReceiptProjection:
    if envelope_type not in {
        "guild.announcement.follow.authorized",
        "guild.announcement.follow.accepted",
        "guild.announcement.follow.finalized",
        "guild.announcement.follow.rejected",
        "guild.announcement.follow.revoked",
        "guild.announcement.follow.updated",
    }:
        raise ValueError("announcement follow receipt has the wrong type")
    if set(content) != {
        "follow_id",
        "generation",
        "source_channel_ref",
        "source_guild_ref",
        "source_channel_name",
        "target_channel_ref",
        "creator_ref",
        "authorization_id",
        "authorization_expires_at",
    }:
        raise ValueError("announcement follow receipt is malformed")
    try:
        raw_follow_id = content["follow_id"]
        raw_generation = content["generation"]
        if (
            not isinstance(raw_follow_id, str)
            or not raw_follow_id.isascii()
            or not raw_follow_id.isdecimal()
            or (len(raw_follow_id) > 1 and raw_follow_id.startswith("0"))
            or not isinstance(raw_generation, str)
            or not raw_generation.isascii()
            or not raw_generation.isdecimal()
            or (len(raw_generation) > 1 and raw_generation.startswith("0"))
        ):
            raise ValueError
        follow_id = int(raw_follow_id)
        generation = int(raw_generation)
        source_ref = EntityRef(str(content["source_channel_ref"]))
        source_guild_ref = EntityRef(str(content["source_guild_ref"]))
        source_channel_name = content["source_channel_name"]
        target_ref = EntityRef(str(content["target_channel_ref"]))
        creator_ref = EntityRef(str(content["creator_ref"]))
        authorization_id = str(content["authorization_id"])
        authorization_expires_at = datetime.fromisoformat(str(content["authorization_expires_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("announcement follow receipt is malformed") from exc
    if not 1 <= follow_id <= MAX_SNOWFLAKE or not 1 <= generation <= MAX_SNOWFLAKE:
        raise ValueError("announcement follow receipt has invalid identifiers")
    resolved_source = source_ref.resolve("invalid.local")
    resolved_source_guild = source_guild_ref.resolve("invalid.local")
    if (
        not isinstance(source_channel_name, str)
        or not 1 <= len(source_channel_name) <= 100
        or source_channel_name != source_channel_name.strip()
        or resolved_source_guild[1] != resolved_source[1]
        or not authorization_id.startswith("kafi_")
        or len(authorization_id) != 48
        or authorization_expires_at.tzinfo is None
    ):
        raise ValueError("announcement follow receipt has invalid source metadata")
    return AnnouncementFollowReceiptProjection(
        follow_id=follow_id,
        generation=generation,
        source_ref=resolved_source,
        source_guild_ref=resolved_source_guild,
        source_channel_name=source_channel_name,
        target_ref=target_ref.resolve("invalid.local"),
        creator_ref=creator_ref.resolve("invalid.local"),
        authorization_id=authorization_id,
        authorization_expires_at=authorization_expires_at,
    )


async def validated_announcement_follow_receipt(
    session: AsyncSession,
    settings: Settings,
    raw: object,
    *,
    expected_type: str,
) -> tuple[dict[str, Any], AnnouncementFollowReceiptProjection]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail={"code": "ANNOUNCEMENT_FOLLOW_RECEIPT_INVALID"})
    try:
        origin = normalize_domain(str(raw.get("origin", "")))
        if origin == settings.domain:
            raise ValueError("a federated receipt must come from the other channel authority")
        envelope = await validated_event_envelope(
            session,
            settings,
            origin,
            raw,
            allow_authority_attested_actor=True,
        )
        if envelope.type != expected_type:
            raise ValueError("announcement follow receipt has the wrong type")
        projection = _announcement_receipt_content(envelope.type, envelope.content)
        context = envelope.context
        raw_guild_id = context.get("guild_id")
        if (
            set(context) != {"guild_id", "guild_domain", "channel_id", "channel_domain"}
            or not isinstance(raw_guild_id, str)
            or not raw_guild_id.isascii()
            or not raw_guild_id.isdecimal()
            or (len(raw_guild_id) > 1 and raw_guild_id.startswith("0"))
            or int(raw_guild_id) > MAX_SNOWFLAKE
            or context.get("guild_domain") != origin
            or context.get("channel_id") != str(projection.target_ref[0])
            or context.get("channel_domain") != projection.target_ref[1]
            or projection.target_ref[1] != origin
        ):
            raise ValueError("announcement follow receipt authority is mismatched")
    except (FederationNetworkError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "ANNOUNCEMENT_FOLLOW_RECEIPT_INVALID"},
        ) from exc
    return envelope.model_dump(mode="json"), projection


def announcement_follow_receipt_content(
    follow: FederatedChannelFollow,
    source: AnnouncementFollowSourceProjection | AnnouncementFollowReceiptProjection,
    *,
    creator_ref: tuple[int, str] | None = None,
) -> dict[str, str]:
    """Build the one closed receipt body shared by every lifecycle phase."""

    if source.authorization_id is None or source.authorization_expires_at is None:
        raise ValueError("announcement follow authorization binding is incomplete")
    if source.generation != follow.generation:
        raise ValueError("announcement follow authorization generation is stale")
    creator = creator_ref or (follow.creator_id, follow.creator_domain)
    return {
        "follow_id": str(follow.id),
        "generation": str(follow.generation),
        "source_channel_ref": f"{follow.source_channel_id}@{follow.source_channel_domain}",
        "source_guild_ref": f"{source.source_guild_ref[0]}@{source.source_guild_ref[1]}",
        "source_channel_name": source.source_channel_name,
        "target_channel_ref": f"{follow.target_channel_id}@{follow.target_channel_domain}",
        "creator_ref": f"{creator[0]}@{creator[1]}",
        "authorization_id": source.authorization_id,
        "authorization_expires_at": source.authorization_expires_at.isoformat(),
    }


def stored_announcement_follow_projection(
    follow: FederatedChannelFollow,
) -> AnnouncementFollowReceiptProjection:
    """Recover the signed source metadata retained with a target-side follow."""

    raw = follow.authority_receipt
    if not isinstance(raw, dict):
        raise ValueError("announcement follow receipt is unavailable")
    event_type = raw.get("type")
    content = raw.get("content")
    if not isinstance(event_type, str) or not isinstance(content, dict):
        raise ValueError("announcement follow receipt is unavailable")
    return _announcement_receipt_content(event_type, cast(dict[str, Any], content))


async def persist_channel_follow_add_message(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    target: ChannelAccess,
    actor: User,
    source: AnnouncementFollowSourceProjection | AnnouncementFollowReceiptProjection,
    *,
    federated_follow: FederatedChannelFollow | None = None,
) -> dict[str, object]:
    """Persist Discord's deletable type-12 notice at the target authority."""

    if (
        target.guild is None
        or target.guild.origin_domain != settings.domain
        or target.channel.origin_domain != settings.domain
        or target.channel.type != 0
        or source.target_ref != (target.channel.id, target.channel.origin_domain)
    ):
        raise RuntimeError("channel follow notice authority binding is invalid")
    reference = build_qualified_message_reference(
        message_type=CHANNEL_FOLLOW_ADD_MESSAGE_TYPE,
        channel_ref=source.source_ref,
        guild_ref=source.source_guild_ref,
    )
    message = Message(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        channel_id=target.channel.id,
        channel_domain=target.channel.origin_domain,
        author_id=actor.id,
        author_domain=actor.origin_domain,
        content=source.source_channel_name,
        e2ee=None,
        encryption_policy_generation=target.channel.encryption_policy_generation,
        encryption_epoch=target.channel.encryption_epoch,
        message_type=CHANNEL_FOLLOW_ADD_MESSAGE_TYPE,
        flags=0,
        message_reference=reference,
        mention_user_refs=[],
        mention_role_refs=[],
        mention_everyone=False,
    )
    session.add(message)
    await session.flush()
    if federated_follow is not None:
        if federated_follow.local_role != "target" or (
            federated_follow.target_channel_id,
            federated_follow.target_channel_domain,
        ) != (target.channel.id, target.channel.origin_domain):
            raise RuntimeError("channel follow notice row binding is invalid")
        federated_follow.notice_message_id = message.id
        federated_follow.notice_message_domain = message.origin_domain
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=message.channel_id,
            channel_domain=message.channel_domain,
            mention_user_refs=[],
        )
    )
    if target.channel.last_message_id is None or (
        target.channel.last_message_id,
        target.channel.last_message_domain or "",
    ) < (message.id, message.origin_domain):
        target.channel.last_message_id = message.id
        target.channel.last_message_domain = message.origin_domain
    rendered = message_payload(message, actor, [])
    await queue_guild_mutation(
        session,
        settings,
        target.guild,
        actor,
        "guild.message.create",
        {
            "message": rendered,
            "author": profile_from_user(actor),
            "thread_starter": False,
        },
        channel=target.channel,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(target.guild.origin_domain, target.guild.id),
        "MESSAGE_CREATE",
        rendered,
    )
    return rendered


async def authorize_federated_announcement_follow_target(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    actor: User,
    application: BotApplication | None,
    source_channel_ref: EntityRef,
    target_channel_ref: EntityRef,
    raw_source_authorization: object,
) -> dict[str, Any]:
    source_ref = source_channel_ref.resolve(settings.domain)
    target = await load_channel_access(session, settings, actor, target_channel_ref)
    if (
        target.guild is None
        or target.guild.origin_domain != settings.domain
        or target.channel.type != 0
        or target.channel.encryption_mode == "e2ee"
        or bool(getattr(target.channel, "e2ee_required", False))
        or source_ref[1] == settings.domain
    ):
        raise HTTPException(status_code=400, detail={"code": "INVALID_ANNOUNCEMENT_FOLLOW"})
    await require_announcement_actor_scope(
        session,
        target,
        actor,
        application,
        "webhooks.manage",
    )
    await require_channel_permissions(
        session,
        redis,
        target,
        actor,
        required_permissions("webhook.manage"),
    )
    source_authorization = await validated_federated_announcement_follow_source_authorization(
        session,
        settings,
        actor,
        source_ref,
        (target.channel.id, target.channel.origin_domain),
        raw_source_authorization,
    )
    authorization_id = source_authorization.authorization_id
    authorization_expires_at = source_authorization.authorization_expires_at
    if authorization_id is None or authorization_expires_at is None:
        raise RuntimeError("validated announcement authorization is incomplete")
    await lock_announcement_mutation(session)
    await lock_webhook_capacity_guild(session, target.guild)
    await session.refresh(target.channel)
    if (
        target.channel.type != 0
        or target.channel.encryption_mode == "e2ee"
        or bool(getattr(target.channel, "e2ee_required", False))
    ):
        raise HTTPException(status_code=400, detail={"code": "INVALID_ANNOUNCEMENT_FOLLOW"})
    await require_announcement_actor_scope(
        session,
        target,
        actor,
        application,
        "webhooks.manage",
    )
    await require_channel_permissions(
        session,
        redis,
        target,
        actor,
        required_permissions("webhook.manage"),
    )
    lock_key = (
        f"announcement-follow:{source_ref[0]}@{source_ref[1]}:"
        f"{target.channel.id}@{target.channel.origin_domain}:target"
    )
    await session.scalar(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))
    follow = await session.scalar(
        select(FederatedChannelFollow)
        .where(
            FederatedChannelFollow.source_channel_id == source_ref[0],
            FederatedChannelFollow.source_channel_domain == source_ref[1],
            FederatedChannelFollow.target_channel_id == target.channel.id,
            FederatedChannelFollow.target_channel_domain == target.channel.origin_domain,
            FederatedChannelFollow.local_role == "target",
        )
        .with_for_update()
    )
    if follow is None and source_authorization.generation != 1:
        raise HTTPException(
            status_code=409,
            detail={"code": "CHANNEL_FOLLOW_AUTHORIZATION_STALE"},
        )
    if follow is not None:
        expected_generation = (
            follow.generation + 1 if follow.lifecycle_state == "revoked" else follow.generation
        )
        if source_authorization.generation != expected_generation:
            raise HTTPException(
                status_code=409,
                detail={"code": "CHANNEL_FOLLOW_AUTHORIZATION_STALE"},
            )
        if follow.lifecycle_state in {"pending", "accepted", "active"}:
            if (
                follow.authorization_id != authorization_id
                or (follow.creator_id, follow.creator_domain) != source_authorization.creator_ref
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "CHANNEL_ALREADY_FOLLOWED"},
                )
            signer = await guild_authority_owner(session, settings, target.guild)
            receipt = await build_guild_authority_envelope(
                session,
                settings,
                target.guild,
                "guild.announcement.follow.authorized",
                signer,
                announcement_follow_receipt_content(follow, source_authorization),
                context={
                    "guild_id": str(target.guild.id),
                    "guild_domain": target.guild.origin_domain,
                    "channel_id": str(target.channel.id),
                    "channel_domain": target.channel.origin_domain,
                },
            )
            # Pending prepares retain the newest signed source expiry so a
            # lost response can be retried after the original five-minute
            # window. Active rows keep their finalization/revocation ledger;
            # the freshly signed authorization is only the idempotent reply.
            if follow.lifecycle_state == "pending":
                follow.authorization_expires_at = authorization_expires_at
                follow.authority_receipt = receipt
            await session.commit()
            return receipt
        if follow.authorization_id == authorization_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "CHANNEL_FOLLOW_AUTHORIZATION_STALE"},
            )
    created_or_reactivated = follow is None or follow.lifecycle_state == "revoked"
    if created_or_reactivated:
        await require_webhook_capacity(
            session,
            target.guild,
            target.channel,
            adding_to_guild=True,
            lock_guild=False,
        )
    if follow is None:
        follow = FederatedChannelFollow(
            id=await snowflake.mint(),
            local_role="target",
            source_channel_id=source_ref[0],
            source_channel_domain=source_ref[1],
            target_channel_id=target.channel.id,
            target_channel_domain=target.channel.origin_domain,
            source_authority_domain=source_ref[1],
            target_authority_domain=settings.domain,
            creator_id=actor.id,
            creator_domain=actor.origin_domain,
            generation=1,
            lifecycle_state="pending",
            authorization_id=authorization_id,
            authorization_expires_at=authorization_expires_at,
            active=False,
            authority_receipt={},
        )
        session.add(follow)
    elif follow.lifecycle_state == "revoked":
        follow.generation += 1
        follow.lifecycle_state = "pending"
        follow.active = False
        follow.revoked_at = None
    follow.creator_id = actor.id
    follow.creator_domain = actor.origin_domain
    if follow.generation != source_authorization.generation:
        raise RuntimeError("announcement follow generation changed while locked")
    follow.authorization_id = authorization_id
    follow.authorization_expires_at = authorization_expires_at
    follow.activated_at = None
    follow.notice_message_id = None
    follow.notice_message_domain = None
    signer = await guild_authority_owner(session, settings, target.guild)
    receipt = await build_guild_authority_envelope(
        session,
        settings,
        target.guild,
        "guild.announcement.follow.authorized",
        signer,
        announcement_follow_receipt_content(follow, source_authorization),
        context={
            "guild_id": str(target.guild.id),
            "guild_domain": target.guild.origin_domain,
            "channel_id": str(target.channel.id),
            "channel_domain": target.channel.origin_domain,
        },
    )
    follow.authority_receipt = receipt
    await session.commit()
    return receipt


async def accept_federated_announcement_follow_source(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    raw_receipt: object,
) -> dict[str, object]:
    _receipt, projection = await validated_announcement_follow_receipt(
        session,
        settings,
        raw_receipt,
        expected_type="guild.announcement.follow.authorized",
    )
    if projection.source_ref[1] != settings.domain:
        raise HTTPException(
            status_code=403,
            detail={"code": "ANNOUNCEMENT_FOLLOW_RECEIPT_MISMATCH"},
        )
    actor = await session.get(User, projection.creator_ref)
    if actor is None or actor.disabled_at is not None:
        raise HTTPException(status_code=403, detail={"code": "USER_NOT_FOUND"})
    application = await announcement_application_for_actor(session, actor)
    source = await load_channel_access(
        session,
        settings,
        actor,
        EntityRef(f"{projection.source_ref[0]}@{projection.source_ref[1]}"),
    )
    if (
        source.guild is None
        or source.channel.type != 5
        or source.channel.encryption_mode == "e2ee"
        or (source.guild.id, source.guild.origin_domain) != projection.source_guild_ref
    ):
        raise HTTPException(status_code=400, detail={"code": "ANNOUNCEMENT_CHANNEL_REQUIRED"})
    await require_announcement_actor_scope(
        session,
        source,
        actor,
        application,
        "channels.read",
    )
    await require_channel_permissions(
        session,
        redis,
        source,
        actor,
        required_permissions("announcement.follow.source"),
    )
    await lock_announcement_mutation(session)
    await session.get(
        Guild,
        (source.guild.id, source.guild.origin_domain),
        with_for_update=True,
    )
    await session.refresh(source.channel)
    if source.channel.type != 5 or source.channel.encryption_mode == "e2ee":
        raise HTTPException(status_code=400, detail={"code": "ANNOUNCEMENT_CHANNEL_REQUIRED"})
    await require_announcement_actor_scope(
        session,
        source,
        actor,
        application,
        "channels.read",
    )
    await require_channel_permissions(
        session,
        redis,
        source,
        actor,
        required_permissions("announcement.follow.source"),
    )
    pair = await session.scalar(
        select(FederatedChannelFollow)
        .where(
            FederatedChannelFollow.source_channel_id == projection.source_ref[0],
            FederatedChannelFollow.source_channel_domain == projection.source_ref[1],
            FederatedChannelFollow.target_channel_id == projection.target_ref[0],
            FederatedChannelFollow.target_channel_domain == projection.target_ref[1],
            FederatedChannelFollow.local_role == "source",
        )
        .with_for_update()
    )
    by_id = await session.get(
        FederatedChannelFollow,
        federated_follow_key(projection.follow_id, projection.target_ref[1], "source"),
        with_for_update=True,
    )
    if (by_id is not None and by_id is not pair) or (
        pair is not None and pair.id != projection.follow_id
    ):
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_FOLLOW_ID_CONFLICT"})
    follow = pair
    if follow is not None:
        exact_retry = (
            follow.authorization_id == projection.authorization_id
            and follow.generation == projection.generation
        )
        fresh_reactivation = (
            follow.lifecycle_state == "revoked"
            and projection.generation == follow.generation + 1
            and follow.authorization_id != projection.authorization_id
        )
        if not (exact_retry or fresh_reactivation):
            raise HTTPException(
                status_code=409,
                detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"},
            )
    if follow is None:
        if projection.generation != 1:
            raise HTTPException(
                status_code=409,
                detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"},
            )
        if projection.authorization_expires_at <= datetime.now(UTC):
            raise HTTPException(
                status_code=409,
                detail={"code": "CHANNEL_FOLLOW_AUTHORIZATION_EXPIRED"},
            )
    if follow is not None and follow.lifecycle_state == "active":
        await session.commit()
        return channel_follow_payload(follow)
    if follow is None:
        follow = FederatedChannelFollow(
            id=projection.follow_id,
            local_role="source",
            source_channel_id=projection.source_ref[0],
            source_channel_domain=projection.source_ref[1],
            target_channel_id=projection.target_ref[0],
            target_channel_domain=projection.target_ref[1],
            source_authority_domain=settings.domain,
            target_authority_domain=projection.target_ref[1],
            creator_id=actor.id,
            creator_domain=actor.origin_domain,
            generation=projection.generation,
            lifecycle_state="accepted",
            authorization_id=projection.authorization_id,
            authorization_expires_at=projection.authorization_expires_at,
            active=False,
            authority_receipt={},
        )
        session.add(follow)
    elif projection.generation < follow.generation:
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"})
    else:
        follow.creator_id = actor.id
        follow.creator_domain = actor.origin_domain
        follow.generation = projection.generation
        follow.lifecycle_state = "accepted"
        follow.active = False
        follow.authorization_id = projection.authorization_id
        follow.authorization_expires_at = projection.authorization_expires_at
        follow.activated_at = None
        follow.revoked_at = None
    signer = await guild_authority_owner(session, settings, source.guild)
    accepted = await build_guild_authority_envelope(
        session,
        settings,
        source.guild,
        "guild.announcement.follow.accepted",
        signer,
        announcement_follow_receipt_content(follow, projection),
        context={
            "guild_id": str(source.guild.id),
            "guild_domain": source.guild.origin_domain,
            "channel_id": str(source.channel.id),
            "channel_domain": source.channel.origin_domain,
        },
    )
    follow.authority_receipt = accepted
    await queue_event(session, settings, projection.target_ref[1], accepted)
    await materialize_updated_at(session, follow)
    rendered = channel_follow_payload(follow)
    await session.commit()
    await enqueue_best_effort(federation_deliver, projection.target_ref[1])
    return rendered


ANNOUNCEMENT_FOLLOW_LIFECYCLE_EVENTS = frozenset(
    {
        "guild.announcement.follow.accepted",
        "guild.announcement.follow.finalized",
        "guild.announcement.follow.rejected",
        "guild.announcement.follow.revoked",
        "guild.announcement.follow.updated",
    }
)


async def lock_announcement_mutation(session: AsyncSession) -> None:
    """Serialize announcement mutations before taking guild/follow row locks."""

    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended("kaede-announcement-mutations", 0)))
    )


async def lock_announcement_guilds(
    session: AsyncSession,
    guild_refs: Collection[tuple[int, str]],
) -> None:
    """Take local guild rows in the shared announcement lock order."""

    for guild_ref in sorted(set(guild_refs), key=lambda ref: (ref[1], ref[0])):
        await session.scalar(
            select(Guild)
            .where(
                Guild.id == guild_ref[0],
                Guild.origin_domain == guild_ref[1],
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )


async def lock_announcement_publish_mutation(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
) -> ChannelAccess:
    """Lock every local guild touched by a publish in deterministic order.

    The advisory lock freezes the active follow set while the target guild
    identities are discovered. Guild locks then precede follow/message locks,
    so opposite-direction announcement channels cannot deadlock and target
    channel policy changes cannot race publication.
    """

    await lock_announcement_mutation(session)
    target_channels = list(
        await session.scalars(
            select(Channel)
            .join(
                ChannelFollow,
                (ChannelFollow.target_channel_id == Channel.id)
                & (ChannelFollow.target_channel_domain == Channel.origin_domain),
            )
            .where(
                ChannelFollow.source_channel_id == access.channel.id,
                ChannelFollow.source_channel_domain == access.channel.origin_domain,
                ChannelFollow.active.is_(True),
            )
        )
    )
    guild = access.guild
    if guild is None:
        return access
    guild_refs = {
        (guild.id, guild.origin_domain),
        *(
            (channel.guild_id, channel.guild_domain)
            for channel in target_channels
            if channel.guild_id is not None and channel.guild_domain == settings.domain
        ),
    }
    await lock_announcement_guilds(session, guild_refs)
    return await lock_local_channel_mutation(session, settings, access)


def _announcement_follow_event_context_matches(
    context: Mapping[str, object],
    *,
    channel_ref: tuple[int, str],
    authority: str,
) -> bool:
    raw_guild_id = context.get("guild_id")
    return bool(
        set(context) == {"guild_id", "guild_domain", "channel_id", "channel_domain"}
        and isinstance(raw_guild_id, str)
        and raw_guild_id.isascii()
        and raw_guild_id.isdecimal()
        and not (len(raw_guild_id) > 1 and raw_guild_id.startswith("0"))
        and int(raw_guild_id) <= MAX_SNOWFLAKE
        and context.get("guild_domain") == authority
        and context.get("channel_id") == str(channel_ref[0])
        and context.get("channel_domain") == channel_ref[1]
    )


async def apply_announcement_follow_lifecycle_event(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    *,
    event_type: str,
    event_origin: str,
    event_timestamp_ms: int,
    event_content: dict[str, Any],
    event_context: dict[str, Any],
    raw_envelope: dict[str, Any],
) -> set[str]:
    """Apply one durable authority hand-off and return outbox destinations to wake."""

    if event_type not in ANNOUNCEMENT_FOLLOW_LIFECYCLE_EVENTS:
        raise ValueError("unsupported announcement follow lifecycle event")
    projection = _announcement_receipt_content(event_type, event_content)
    await lock_announcement_mutation(session)

    if event_type == "guild.announcement.follow.accepted":
        if (
            event_origin != projection.source_ref[1]
            or projection.target_ref[1] != settings.domain
            or not _announcement_follow_event_context_matches(
                event_context,
                channel_ref=projection.source_ref,
                authority=event_origin,
            )
            or datetime.fromtimestamp(event_timestamp_ms / 1000, tz=UTC)
            > projection.authorization_expires_at
        ):
            raise ValueError("announcement acceptance authority is invalid")
        actor = await session.get(User, projection.creator_ref)
        target = (
            await load_channel_access(
                session,
                settings,
                actor,
                EntityRef(f"{projection.target_ref[0]}@{projection.target_ref[1]}"),
            )
            if actor is not None
            else None
        )
        if actor is None or target is None or target.guild is None:
            raise ValueError("announcement acceptance actor or target is unavailable")
        await lock_webhook_capacity_guild(session, target.guild)
        await session.refresh(target.channel)
        follow = await session.get(
            FederatedChannelFollow,
            federated_follow_key(projection.follow_id, projection.target_ref[1], "target"),
            with_for_update=True,
        )
        if follow is None or (
            follow.source_channel_id,
            follow.source_channel_domain,
            follow.target_channel_id,
            follow.target_channel_domain,
            follow.creator_id,
            follow.creator_domain,
            follow.generation,
            follow.authorization_id,
        ) != (
            *projection.source_ref,
            *projection.target_ref,
            *projection.creator_ref,
            projection.generation,
            projection.authorization_id,
        ):
            raise ValueError("announcement acceptance does not match its prepared follow")
        if follow.lifecycle_state == "active":
            return set()
        if follow.lifecycle_state != "pending":
            raise ValueError("announcement acceptance is stale")

        rejection_code: str | None = None
        application: BotApplication | None = None
        try:
            if (
                target.guild.origin_domain != settings.domain
                or target.channel.origin_domain != settings.domain
                or target.channel.type != 0
                or target.channel.encryption_mode == "e2ee"
                or bool(getattr(target.channel, "e2ee_required", False))
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "INVALID_ANNOUNCEMENT_FOLLOW"},
                )
            application = await announcement_application_for_actor(session, actor)
            await require_announcement_actor_scope(
                session,
                target,
                actor,
                application,
                "webhooks.manage",
            )
            await require_channel_permissions(
                session,
                redis,
                target,
                actor,
                required_permissions("webhook.manage"),
            )
            await require_webhook_capacity(
                session,
                target.guild,
                target.channel,
                adding_to_guild=True,
                lock_guild=False,
            )
        except HTTPException as exc:
            detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
            rejection_code = str(detail.get("code", "ANNOUNCEMENT_FOLLOW_FINALIZE_DENIED"))

        signer = await guild_authority_owner(session, settings, target.guild)
        if rejection_code is not None:
            follow.lifecycle_state = "revoked"
            follow.active = False
            follow.revoked_at = datetime.now(UTC)
            rejected = await build_guild_authority_envelope(
                session,
                settings,
                target.guild,
                "guild.announcement.follow.rejected",
                signer,
                announcement_follow_receipt_content(follow, projection),
                context={
                    "guild_id": str(target.guild.id),
                    "guild_domain": target.guild.origin_domain,
                    "channel_id": str(target.channel.id),
                    "channel_domain": target.channel.origin_domain,
                },
            )
            follow.authority_receipt = rejected
            await queue_event(session, settings, projection.source_ref[1], rejected)
            return {projection.source_ref[1]}

        await persist_channel_follow_add_message(
            session,
            settings,
            snowflake,
            target,
            actor,
            projection,
            federated_follow=follow,
        )
        follow.lifecycle_state = "active"
        follow.active = True
        follow.activated_at = datetime.now(UTC)
        follow.revoked_at = None
        finalized = await build_guild_authority_envelope(
            session,
            settings,
            target.guild,
            "guild.announcement.follow.finalized",
            signer,
            announcement_follow_receipt_content(follow, projection),
            context={
                "guild_id": str(target.guild.id),
                "guild_domain": target.guild.origin_domain,
                "channel_id": str(target.channel.id),
                "channel_domain": target.channel.origin_domain,
            },
        )
        await queue_event(session, settings, projection.source_ref[1], finalized)
        queue_postcommit_dispatch(
            session,
            guild_topic(target.guild.origin_domain, target.guild.id),
            "WEBHOOKS_UPDATE",
            {
                "guild_id": str(target.guild.id),
                "guild_domain": target.guild.origin_domain,
                "channel_id": str(target.channel.id),
                "channel_domain": target.channel.origin_domain,
            },
        )
        return {projection.source_ref[1]}

    if (
        event_origin != projection.target_ref[1]
        or projection.source_ref[1] != settings.domain
        or not _announcement_follow_event_context_matches(
            event_context,
            channel_ref=projection.target_ref,
            authority=event_origin,
        )
    ):
        raise ValueError("announcement lifecycle target authority is invalid")
    source_channel = await session.get(Channel, projection.source_ref)
    if source_channel is None or source_channel.guild_id is None:
        raise ValueError("announcement lifecycle source channel is unavailable")
    source_guild = await session.get(
        Guild,
        (source_channel.guild_id, source_channel.guild_domain),
        with_for_update=True,
    )
    if (
        source_guild is None
        or source_channel.type != 5
        or (source_guild.id, source_guild.origin_domain) != projection.source_guild_ref
    ):
        raise ValueError("announcement lifecycle source binding is invalid")
    follow = await session.get(
        FederatedChannelFollow,
        federated_follow_key(projection.follow_id, projection.target_ref[1], "source"),
        with_for_update=True,
    )
    if event_type == "guild.announcement.follow.updated":
        if follow is None or (
            follow.source_channel_id,
            follow.source_channel_domain,
            follow.creator_id,
            follow.creator_domain,
            follow.authorization_id,
            follow.generation,
        ) != (
            *projection.source_ref,
            *projection.creator_ref,
            projection.authorization_id,
            projection.generation,
        ):
            raise ValueError("announcement follow update does not match its source follow")
        if follow.lifecycle_state != "active" or not follow.active:
            raise ValueError("announcement follow update is stale")
        follow.target_channel_id = projection.target_ref[0]
        follow.target_channel_domain = projection.target_ref[1]
        follow.target_authority_domain = projection.target_ref[1]
        follow.authority_receipt = raw_envelope
        return set()
    if follow is None:
        if event_type not in {
            "guild.announcement.follow.rejected",
            "guild.announcement.follow.revoked",
        }:
            raise ValueError("announcement lifecycle event has no source follow")
        creator = await session.get(User, projection.creator_ref)
        if creator is None:
            raise ValueError("announcement lifecycle creator is unavailable")
        # A target may reject or revoke an orphaned prepare before the source
        # accepted it. Keep a source-side tombstone so the next authorization
        # advances the signed generation instead of replaying the stale one.
        follow = FederatedChannelFollow(
            id=projection.follow_id,
            local_role="source",
            source_channel_id=projection.source_ref[0],
            source_channel_domain=projection.source_ref[1],
            target_channel_id=projection.target_ref[0],
            target_channel_domain=projection.target_ref[1],
            source_authority_domain=projection.source_ref[1],
            target_authority_domain=projection.target_ref[1],
            creator_id=projection.creator_ref[0],
            creator_domain=projection.creator_ref[1],
            generation=projection.generation,
            lifecycle_state="revoked",
            authorization_id=projection.authorization_id,
            authorization_expires_at=projection.authorization_expires_at,
            active=False,
            revoked_at=datetime.now(UTC),
            authority_receipt=raw_envelope,
        )
        session.add(follow)
        return set()
    if (
        follow.source_channel_id,
        follow.source_channel_domain,
        follow.target_channel_id,
        follow.target_channel_domain,
        follow.creator_id,
        follow.creator_domain,
        follow.authorization_id,
    ) != (
        *projection.source_ref,
        *projection.target_ref,
        *projection.creator_ref,
        projection.authorization_id,
    ):
        raise ValueError("announcement lifecycle event does not match its source follow")

    if event_type == "guild.announcement.follow.finalized":
        if projection.generation != follow.generation:
            raise ValueError("announcement finalization generation is stale")
        if follow.lifecycle_state == "active":
            return set()
        if follow.lifecycle_state != "accepted":
            raise ValueError("announcement finalization state is stale")
        follow.lifecycle_state = "active"
        follow.active = True
        follow.activated_at = datetime.now(UTC)
    else:
        expected_generation = (
            follow.generation + 1
            if event_type == "guild.announcement.follow.revoked"
            else follow.generation
        )
        if projection.generation != expected_generation:
            raise ValueError("announcement revocation generation is stale")
        follow.generation = projection.generation
        follow.lifecycle_state = "revoked"
        follow.active = False
        follow.revoked_at = datetime.now(UTC)
    follow.authority_receipt = raw_envelope
    return set()


async def reconcile_expired_announcement_follow_prepares(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> set[str]:
    """Reject abandoned target prepares after a bounded delivery grace period."""

    current = now or datetime.now(UTC)
    cutoff = current - timedelta(hours=24)
    await lock_announcement_mutation(session)
    candidate_refs = list(
        (
            await session.execute(
                select(
                    FederatedChannelFollow.id,
                    FederatedChannelFollow.target_authority_domain,
                    FederatedChannelFollow.target_channel_id,
                    FederatedChannelFollow.target_channel_domain,
                )
                .where(
                    FederatedChannelFollow.local_role == "target",
                    FederatedChannelFollow.lifecycle_state == "pending",
                    FederatedChannelFollow.authorization_expires_at < cutoff,
                )
                .order_by(FederatedChannelFollow.authorization_expires_at)
                .limit(limit)
            )
        ).tuples()
    )
    channels: dict[tuple[int, str], tuple[Channel, Guild]] = {}
    for follow_id, authority_domain, channel_id, channel_domain in candidate_refs:
        channel = await session.get(Channel, (channel_id, channel_domain))
        if channel is None or channel.guild_id is None:
            continue
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is not None and guild.origin_domain == settings.domain:
            channels[(int(follow_id), str(authority_domain))] = (channel, guild)
    for guild_ref in sorted(
        {(guild.id, guild.origin_domain) for _channel, guild in channels.values()},
        key=lambda ref: (ref[1], ref[0]),
    ):
        await session.get(Guild, guild_ref, with_for_update=True)

    destinations: set[str] = set()
    for follow_key in sorted(channels):
        channel, guild = channels[follow_key]
        follow = await session.get(
            FederatedChannelFollow,
            (*follow_key, "target"),
            with_for_update=True,
            populate_existing=True,
        )
        if (
            follow is None
            or follow.lifecycle_state != "pending"
            or follow.authorization_expires_at is None
            or follow.authorization_expires_at >= cutoff
        ):
            continue
        try:
            projection = _announcement_receipt_content(
                str(follow.authority_receipt.get("type", "")),
                cast(dict[str, Any], follow.authority_receipt.get("content")),
            )
        except (AttributeError, TypeError, ValueError):
            continue
        follow.lifecycle_state = "revoked"
        follow.active = False
        follow.revoked_at = current
        signer = await guild_authority_owner(session, settings, guild)
        rejected = await build_guild_authority_envelope(
            session,
            settings,
            guild,
            "guild.announcement.follow.rejected",
            signer,
            announcement_follow_receipt_content(follow, projection),
            context={
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
            },
        )
        await queue_event(session, settings, follow.source_authority_domain, rejected)
        destinations.add(follow.source_authority_domain)
    await session.commit()
    return destinations


async def revoke_federated_announcement_follow_target(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    application: BotApplication | None,
    follow_id: int,
    generation: int,
) -> dict[str, Any]:
    unlocked_follow = await session.get(
        FederatedChannelFollow,
        federated_follow_key(follow_id, settings.domain, "target"),
    )
    if unlocked_follow is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
    target = await load_channel_access(
        session,
        settings,
        actor,
        EntityRef(f"{unlocked_follow.target_channel_id}@{unlocked_follow.target_channel_domain}"),
    )
    if target.guild is None or target.guild.origin_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
    await lock_announcement_mutation(session)
    await lock_webhook_capacity_guild(session, target.guild)
    await session.refresh(target.channel)
    follow = await session.get(
        FederatedChannelFollow,
        federated_follow_key(follow_id, settings.domain, "target"),
        with_for_update=True,
        populate_existing=True,
    )
    if follow is None or (
        follow.target_channel_id,
        follow.target_channel_domain,
    ) != (target.channel.id, target.channel.origin_domain):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
    await require_announcement_actor_scope(
        session,
        target,
        actor,
        application,
        "webhooks.manage",
    )
    await require_channel_permissions(
        session,
        redis,
        target,
        actor,
        required_permissions("webhook.manage"),
    )
    if follow.lifecycle_state == "revoked":
        try:
            stored_type = str(follow.authority_receipt.get("type", ""))
        except AttributeError:
            stored_type = ""
        if follow.generation == generation + 1 and stored_type == (
            "guild.announcement.follow.revoked"
        ):
            return dict(follow.authority_receipt)
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"})
    if follow.generation != generation:
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"})
    try:
        prior_projection = _announcement_receipt_content(
            str(follow.authority_receipt.get("type", "")),
            cast(dict[str, Any], follow.authority_receipt.get("content")),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"},
        ) from exc
    was_active = follow.active
    follow.active = False
    follow.lifecycle_state = "revoked"
    follow.revoked_at = datetime.now(UTC)
    follow.generation += 1
    previous_avatar = await detach_announcement_follower_avatar(session, follow)
    signer = await guild_authority_owner(session, settings, target.guild)
    receipt = await build_guild_authority_envelope(
        session,
        settings,
        target.guild,
        "guild.announcement.follow.revoked",
        signer,
        announcement_follow_receipt_content(
            follow,
            replace(prior_projection, generation=follow.generation),
        ),
        context={
            "guild_id": str(target.guild.id),
            "guild_domain": target.guild.origin_domain,
            "channel_id": str(target.channel.id),
            "channel_domain": target.channel.origin_domain,
        },
    )
    follow.authority_receipt = receipt
    await queue_event(session, settings, follow.source_authority_domain, receipt)
    await session.commit()
    await enqueue_best_effort(federation_deliver, follow.source_authority_domain)
    if previous_avatar is not None:
        await enqueue_best_effort(
            media_local_purge,
            previous_avatar.id,
            previous_avatar.origin_domain,
        )
    if was_active:
        await publish_follower_webhook_update(redis, target.guild, target.channel)
    return receipt


async def deactivate_federated_announcement_follow_source(
    session: AsyncSession,
    settings: Settings,
    raw_receipt: object,
) -> None:
    receipt, projection = await validated_announcement_follow_receipt(
        session,
        settings,
        raw_receipt,
        expected_type="guild.announcement.follow.revoked",
    )
    if projection.source_ref[1] != settings.domain:
        raise HTTPException(
            status_code=403,
            detail={"code": "ANNOUNCEMENT_FOLLOW_RECEIPT_MISMATCH"},
        )
    source_channel = await session.get(Channel, projection.source_ref)
    if (
        source_channel is None
        or source_channel.unavailable
        or source_channel.type != 5
        or source_channel.guild_domain != settings.domain
        or (source_channel.guild_id, source_channel.guild_domain) != projection.source_guild_ref
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
    await lock_announcement_mutation(session)
    await session.get(
        Guild,
        (source_channel.guild_id, source_channel.guild_domain),
        with_for_update=True,
    )
    follow = await session.get(
        FederatedChannelFollow,
        federated_follow_key(projection.follow_id, projection.target_ref[1], "source"),
        with_for_update=True,
    )
    if follow is None or (
        follow.source_channel_id,
        follow.source_channel_domain,
        follow.target_channel_id,
        follow.target_channel_domain,
    ) != (*projection.source_ref, *projection.target_ref):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
    if (follow.creator_id, follow.creator_domain) != projection.creator_ref:
        raise HTTPException(
            status_code=403,
            detail={"code": "ANNOUNCEMENT_FOLLOW_RECEIPT_MISMATCH"},
        )
    if projection.generation < follow.generation or projection.generation > follow.generation + 1:
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"})
    if projection.generation == follow.generation and follow.active:
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_FOLLOW_RECEIPT_STALE"})
    follow.generation = projection.generation
    follow.active = False
    follow.lifecycle_state = "revoked"
    follow.revoked_at = datetime.now(UTC)
    follow.authority_receipt = receipt
    await session.commit()


async def delete_announcement_follow_from_target(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    application: BotApplication | None,
    follow: ChannelFollow | FederatedChannelFollow,
) -> tuple[Guild, Channel]:
    """Unfollow an announcement channel through its follower webhook.

    Discord exposes an announcement subscription as a type-2 webhook in the
    destination channel.  Deleting that webhook is therefore a target-guild
    operation: the target authority rechecks the actor and, for a federated
    source, sends the target authority's signed revocation receipt to the
    source authority through its durable outbox. The source must not require
    the target moderator to retain membership there or be online synchronously.
    """

    target = await load_channel_access(
        session,
        settings,
        actor,
        EntityRef(f"{follow.target_channel_id}@{follow.target_channel_domain}"),
    )
    if (
        target.guild is None
        or target.guild.origin_domain != settings.domain
        or target.channel.origin_domain != settings.domain
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
    await require_announcement_actor_scope(
        session,
        target,
        actor,
        application,
        "webhooks.manage",
    )
    await require_channel_permissions(
        session,
        redis,
        target,
        actor,
        required_permissions("webhook.manage"),
    )
    if not follow.active:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})

    if isinstance(follow, ChannelFollow):
        await lock_announcement_mutation(session)
        await session.get(
            Guild,
            (target.guild.id, target.guild.origin_domain),
            with_for_update=True,
        )
        await session.refresh(target.channel)
        locked_follow = await session.get(
            ChannelFollow,
            follow.id,
            with_for_update=True,
            populate_existing=True,
        )
        if locked_follow is None or not locked_follow.active:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
        await require_announcement_actor_scope(
            session,
            target,
            actor,
            application,
            "webhooks.manage",
        )
        await require_channel_permissions(
            session,
            redis,
            target,
            actor,
            required_permissions("webhook.manage"),
        )
        locked_follow.active = False
        previous_avatar = await detach_announcement_follower_avatar(
            session,
            locked_follow,
        )
        await session.commit()
        await publish_follower_webhook_update(redis, target.guild, target.channel)
        if previous_avatar is not None:
            await enqueue_best_effort(
                media_local_purge,
                previous_avatar.id,
                previous_avatar.origin_domain,
            )
        return target.guild, target.channel

    if follow.local_role != "target":
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
    generation = follow.generation
    await revoke_federated_announcement_follow_target(
        session,
        redis,
        settings,
        actor,
        application,
        follow.id,
        generation,
    )
    return target.guild, target.channel


@router.post("/{channel_id}/followers")
async def follow_announcement_channel(
    channel_id: EntityRef,
    payload: ChannelFollowCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    source = await load_channel_access(session, settings, auth.user, channel_id)
    target = await load_channel_access(session, settings, auth.user, payload.target_channel_id)
    application = announcement_actor_application(auth)
    if source.channel.type != 5 or target.channel.type != 0:
        raise HTTPException(status_code=400, detail={"code": "INVALID_ANNOUNCEMENT_FOLLOW"})
    if source.guild is None or target.guild is None:
        raise HTTPException(status_code=400, detail={"code": "INVALID_ANNOUNCEMENT_FOLLOW"})
    if (
        source.channel.encryption_mode == "e2ee"
        or target.channel.encryption_mode == "e2ee"
        or bool(getattr(target.channel, "e2ee_required", False))
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_CROSSPOST_UNSUPPORTED"})
    await require_announcement_actor_scope(
        session,
        source,
        auth.user,
        application,
        "channels.read",
    )
    await require_announcement_actor_scope(
        session,
        target,
        auth.user,
        application,
        "webhooks.manage",
    )
    await require_channel_permissions(
        session,
        redis,
        source,
        auth.user,
        required_permissions("announcement.follow.source"),
    )
    await require_channel_permissions(
        session,
        redis,
        target,
        auth.user,
        required_permissions("webhook.manage"),
    )
    source_authority = source.guild.origin_domain
    target_authority = target.guild.origin_domain
    if source_authority != settings.domain or target_authority != settings.domain:
        actor_profile = profile_from_user(auth.user)
        actor_application_ref = (
            f"{application.id}@{application.origin_domain}" if application is not None else None
        )
        source_actor_intent = actor_intent_for_authority(
            payload.actor_intent,
            payload.actor_intents,
            source_authority,
        )
        target_actor_intent = actor_intent_for_authority(
            payload.actor_intent,
            payload.actor_intents,
            target_authority,
        )
        if source_authority == target_authority:
            upstream = await signed_request(
                session,
                settings,
                "POST",
                source_authority,
                f"/_kaede/v1/channels/{source.channel.id}/announcement-follow-create",
                payload={
                    "actor": actor_profile,
                    "actor_application_ref": actor_application_ref,
                    "actor_intent": source_actor_intent,
                    "source_channel_ref": (f"{source.channel.id}@{source.channel.origin_domain}"),
                    "target_channel_id": str(target.channel.id),
                },
                request_timeout=10,
                max_response_bytes=64 * 1024,
            )
            if upstream.status_code != 201:
                detail = {"code": "FEDERATED_ANNOUNCEMENT_FOLLOW_FAILED"}
                raw_error = decode_federation_response_json(upstream)
                if isinstance(raw_error, dict) and isinstance(raw_error.get("detail"), dict):
                    detail = raw_error["detail"]
                raise HTTPException(status_code=upstream.status_code, detail=detail)
            raw_follow = decode_federation_response_json(upstream)
            try:
                return validate_channel_follow_response(
                    raw_follow,
                    source_ref=(source.channel.id, source.channel.origin_domain),
                    target_ref=(target.channel.id, target.channel.origin_domain),
                    creator_ref=(auth.user.id, auth.user.origin_domain),
                )
            except (FederationNetworkError, TypeError, ValueError):
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATED_ANNOUNCEMENT_FOLLOW_INVALID"},
                ) from None

        source_ref = (source.channel.id, source.channel.origin_domain)
        target_ref = (target.channel.id, target.channel.origin_domain)
        if source_authority == settings.domain:
            source_authorization: object = await authorize_federated_announcement_follow_source(
                session,
                redis,
                settings,
                auth.user,
                application,
                EntityRef(f"{source_ref[0]}@{source_ref[1]}"),
                EntityRef(f"{target_ref[0]}@{target_ref[1]}"),
            )
        else:
            source_authorization = await request_federated_announcement_follow_source_authorization(
                session,
                settings,
                auth.user,
                application,
                source_ref,
                target_ref,
                source_actor_intent,
            )

        receipt: object
        if target_authority == settings.domain:
            receipt = await authorize_federated_announcement_follow_target(
                session,
                redis,
                snowflake,
                settings,
                auth.user,
                application,
                EntityRef(f"{source.channel.id}@{source.channel.origin_domain}"),
                EntityRef(f"{target.channel.id}@{target.channel.origin_domain}"),
                source_authorization,
            )
        else:
            authorized = await signed_request(
                session,
                settings,
                "POST",
                target_authority,
                f"/_kaede/v1/channels/{target.channel.id}/announcement-follow-authorize",
                payload={
                    "actor": actor_profile,
                    "actor_application_ref": actor_application_ref,
                    "actor_intent": target_actor_intent,
                    "source_channel_ref": (f"{source.channel.id}@{source.channel.origin_domain}"),
                    "target_channel_id": str(target.channel.id),
                    "source_authorization": source_authorization,
                },
                request_timeout=10,
                max_response_bytes=64 * 1024,
            )
            if authorized.status_code != 201:
                detail = {"code": "ANNOUNCEMENT_FOLLOW_AUTHORIZATION_FAILED"}
                raw_error = decode_federation_response_json(authorized)
                if isinstance(raw_error, dict) and isinstance(raw_error.get("detail"), dict):
                    detail = raw_error["detail"]
                raise HTTPException(status_code=authorized.status_code, detail=detail)
            receipt = decode_federation_response_json(authorized)
        if source_authority == settings.domain:
            return await accept_federated_announcement_follow_source(
                session,
                redis,
                settings,
                receipt,
            )
        accepted = await signed_request(
            session,
            settings,
            "POST",
            source_authority,
            f"/_kaede/v1/channels/{source.channel.id}/announcement-follow-accept",
            payload={
                "receipt": receipt,
            },
            request_timeout=10,
            max_response_bytes=64 * 1024,
        )
        if accepted.status_code != 201:
            detail = {"code": "ANNOUNCEMENT_FOLLOW_SUBSCRIPTION_FAILED"}
            raw_error = decode_federation_response_json(accepted)
            if isinstance(raw_error, dict) and isinstance(raw_error.get("detail"), dict):
                detail = raw_error["detail"]
            raise HTTPException(status_code=accepted.status_code, detail=detail)
        raw_follow = decode_federation_response_json(accepted)
        try:
            return validate_channel_follow_response(
                raw_follow,
                source_ref=(source.channel.id, source.channel.origin_domain),
                target_ref=(target.channel.id, target.channel.origin_domain),
                creator_ref=(auth.user.id, auth.user.origin_domain),
            )
        except (FederationNetworkError, TypeError, ValueError):
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_ANNOUNCEMENT_FOLLOW_INVALID"},
            ) from None
    await lock_announcement_mutation(session)
    for guild_ref in sorted(
        {
            (source.guild.id, source.guild.origin_domain),
            (target.guild.id, target.guild.origin_domain),
        },
        key=lambda ref: (ref[1], ref[0]),
    ):
        await session.get(Guild, guild_ref, with_for_update=True)
    await session.refresh(source.channel)
    await session.refresh(target.channel)
    if (
        source.channel.type != 5
        or target.channel.type != 0
        or source.channel.encryption_mode == "e2ee"
        or target.channel.encryption_mode == "e2ee"
        or bool(getattr(target.channel, "e2ee_required", False))
    ):
        raise HTTPException(status_code=409, detail={"code": "INVALID_ANNOUNCEMENT_FOLLOW"})
    await require_announcement_actor_scope(
        session,
        source,
        auth.user,
        application,
        "channels.read",
    )
    await require_announcement_actor_scope(
        session,
        target,
        auth.user,
        application,
        "webhooks.manage",
    )
    await require_channel_permissions(
        session,
        redis,
        source,
        auth.user,
        required_permissions("announcement.follow.source"),
    )
    await require_channel_permissions(
        session,
        redis,
        target,
        auth.user,
        required_permissions("webhook.manage"),
    )
    await lock_webhook_capacity_guild(session, target.guild)
    existing_follow = await session.scalar(
        select(ChannelFollow)
        .where(
            ChannelFollow.source_channel_id == source.channel.id,
            ChannelFollow.source_channel_domain == source.channel.origin_domain,
            ChannelFollow.target_channel_id == target.channel.id,
            ChannelFollow.target_channel_domain == target.channel.origin_domain,
        )
        .with_for_update()
    )
    created_or_reactivated = existing_follow is None or not existing_follow.active
    if created_or_reactivated:
        await require_webhook_capacity(
            session,
            target.guild,
            target.channel,
            adding_to_guild=True,
            lock_guild=False,
        )
    if existing_follow is None:
        follow = ChannelFollow(
            id=await snowflake.mint(),
            source_channel_id=source.channel.id,
            source_channel_domain=source.channel.origin_domain,
            target_channel_id=target.channel.id,
            target_channel_domain=target.channel.origin_domain,
            creator_id=auth.user.id,
            creator_domain=auth.user.origin_domain,
            active=True,
        )
        session.add(follow)
    else:
        follow = existing_follow
        follow.creator_id = auth.user.id
        follow.creator_domain = auth.user.origin_domain
        follow.active = True
    if created_or_reactivated:
        await persist_channel_follow_add_message(
            session,
            settings,
            snowflake,
            target,
            auth.user,
            AnnouncementFollowSourceProjection(
                source_ref=(source.channel.id, source.channel.origin_domain),
                source_guild_ref=(source.guild.id, source.guild.origin_domain),
                source_channel_name=source.channel.name or "announcements",
                target_ref=(target.channel.id, target.channel.origin_domain),
                creator_ref=(auth.user.id, auth.user.origin_domain),
            ),
        )
    await materialize_updated_at(session, follow)
    rendered = channel_follow_payload(follow)
    await session.commit()
    if created_or_reactivated:
        await wake_queued_guild_federation(target.guild)
    await publish_follower_webhook_update(redis, target.guild, target.channel)
    return rendered


@router.get("/{channel_id}/followers")
async def list_announcement_follows(
    channel_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    *,
    actor_intent: dict[str, object] | None = None,
    actor_intents: Mapping[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    source = await load_channel_access(session, settings, auth.user, channel_id)
    application = announcement_actor_application(auth)
    await require_announcement_actor_scope(
        session,
        source,
        auth.user,
        application,
        "channels.read",
    )
    await require_channel_permissions(
        session,
        redis,
        source,
        auth.user,
        required_permissions("announcement.follow.source"),
    )
    if source.guild is not None and source.guild.origin_domain != settings.domain:
        upstream = await signed_request(
            session,
            settings,
            "POST",
            source.guild.origin_domain,
            f"/_kaede/v1/channels/{source.channel.id}/announcement-follow-list",
            payload={
                "actor": profile_from_user(auth.user),
                "actor_application_ref": (
                    f"{application.id}@{application.origin_domain}"
                    if application is not None
                    else None
                ),
                "actor_intent": actor_intent_for_authority(
                    actor_intent,
                    actor_intents,
                    source.guild.origin_domain,
                ),
            },
            request_timeout=10,
            max_response_bytes=256 * 1024,
        )
        if upstream.status_code != 200:
            raise HTTPException(
                status_code=upstream.status_code,
                detail={"code": "FEDERATED_ANNOUNCEMENT_FOLLOWS_UNAVAILABLE"},
            )
        raw = decode_federation_response_json(upstream)
        try:
            return validate_channel_follow_page(
                raw,
                source_ref=(source.channel.id, source.channel.origin_domain),
            )
        except (FederationNetworkError, TypeError, ValueError):
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_ANNOUNCEMENT_FOLLOWS_INVALID"},
            ) from None
    follows = list(
        await session.scalars(
            select(ChannelFollow)
            .where(
                ChannelFollow.source_channel_id == source.channel.id,
                ChannelFollow.source_channel_domain == source.channel.origin_domain,
                ChannelFollow.active.is_(True),
            )
            .order_by(ChannelFollow.id)
        )
    )
    federated = list(
        await session.scalars(
            select(FederatedChannelFollow)
            .where(
                FederatedChannelFollow.source_channel_id == source.channel.id,
                FederatedChannelFollow.source_channel_domain == source.channel.origin_domain,
                FederatedChannelFollow.local_role == "source",
                FederatedChannelFollow.active.is_(True),
            )
            .order_by(
                FederatedChannelFollow.id,
                FederatedChannelFollow.target_authority_domain,
            )
        )
    )
    rendered = [
        *[channel_follow_payload(follow) for follow in follows],
        *[channel_follow_payload(follow) for follow in federated],
    ]
    rendered.sort(
        key=lambda item: (
            validate_snowflake(item["id"]),
            EntityRef(str(item["ref"])).resolve(settings.domain)[1],
        )
    )
    return validate_channel_follow_page(
        rendered,
        source_ref=(source.channel.id, source.channel.origin_domain),
    )


async def source_announcement_follow(
    session: AsyncSession,
    source_ref: tuple[int, str],
    follow_ref: EntityRef,
    *,
    local_domain: str,
) -> ChannelFollow | FederatedChannelFollow | None:
    """Resolve a source-side follow without collapsing target-owned IDs."""

    requested = follow_ref.reference
    candidates: list[ChannelFollow | FederatedChannelFollow] = []
    if requested.domain in {None, local_domain}:
        ordinary = await session.get(ChannelFollow, requested.id)
        if (
            ordinary is not None
            and (
                ordinary.source_channel_id,
                ordinary.source_channel_domain,
            )
            == source_ref
        ):
            candidates.append(ordinary)
    statement = select(FederatedChannelFollow).where(
        FederatedChannelFollow.id == requested.id,
        FederatedChannelFollow.source_channel_id == source_ref[0],
        FederatedChannelFollow.source_channel_domain == source_ref[1],
        FederatedChannelFollow.local_role == "source",
    )
    if requested.domain is not None:
        statement = statement.where(
            FederatedChannelFollow.target_authority_domain == requested.domain
        )
    candidates.extend(await session.scalars(statement))
    if len(candidates) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CHANNEL_FOLLOW_REF_REQUIRED",
                "message": "Use the qualified follow ref returned by the follower list.",
            },
        )
    return candidates[0] if candidates else None


@router.delete("/{channel_id}/followers/{follow_ref}", status_code=204)
async def delete_announcement_follow(
    channel_id: EntityRef,
    follow_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    actor_intent_header: Annotated[str | None, Header(alias="X-Kaede-Actor-Intent")] = None,
    actor_intents_header: Annotated[str | None, Header(alias="X-Kaede-Actor-Intents")] = None,
    *,
    actor_intent: dict[str, object] | None = None,
    actor_intents: Mapping[str, dict[str, object]] | None = None,
) -> Response:
    if actor_intent is None and actor_intents is None:
        try:
            actor_intent, actor_intents = parse_actor_intent_headers(
                actor_intent_header,
                actor_intents_header,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "ACTOR_INTENT_INVALID"},
            ) from exc
    source = await load_channel_access(session, settings, auth.user, channel_id)
    application = announcement_actor_application(auth)
    await require_announcement_actor_scope(
        session,
        source,
        auth.user,
        application,
        "channels.read",
    )
    source_authority = source.guild.origin_domain if source.guild is not None else settings.domain
    follow = (
        await source_announcement_follow(
            session,
            (source.channel.id, source.channel.origin_domain),
            follow_ref,
            local_domain=settings.domain,
        )
        if source_authority == settings.domain
        else None
    )
    ordinary = follow if isinstance(follow, ChannelFollow) else None
    federated = follow if isinstance(follow, FederatedChannelFollow) else None
    if source_authority != settings.domain:
        source_intent = actor_intent_for_authority(
            actor_intent,
            actor_intents,
            source_authority,
        )
        upstream = await signed_request(
            session,
            settings,
            "POST",
            source_authority,
            (f"/_kaede/v1/channels/{source.channel.id}/announcement-follow-delete/{follow_ref}"),
            payload={
                "actor": profile_from_user(auth.user),
                "actor_application_ref": (
                    f"{application.id}@{application.origin_domain}"
                    if application is not None
                    else None
                ),
                "actor_intent": source_intent if not actor_intents else None,
                "actor_intents": dict(actor_intents or {}),
            },
            request_timeout=10,
            max_response_bytes=64 * 1024,
        )
        if upstream.status_code != 204:
            raise HTTPException(
                status_code=upstream.status_code,
                detail={"code": "FEDERATED_ANNOUNCEMENT_FOLLOW_DELETE_FAILED"},
            )
        return Response(status_code=204)
    if ordinary is not None and (
        ordinary.source_channel_id,
        ordinary.source_channel_domain,
    ) == (source.channel.id, source.channel.origin_domain):
        target_ref = (ordinary.target_channel_id, ordinary.target_channel_domain)
        generation: int | None = None
    elif federated is not None and (
        federated.source_channel_id,
        federated.source_channel_domain,
    ) == (source.channel.id, source.channel.origin_domain):
        target_ref = (federated.target_channel_id, federated.target_channel_domain)
        generation = federated.generation
    else:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
    follow_id = follow_ref.id
    target = await load_channel_access(
        session,
        settings,
        auth.user,
        EntityRef(f"{target_ref[0]}@{target_ref[1]}"),
    )
    await require_announcement_actor_scope(
        session,
        target,
        auth.user,
        application,
        "webhooks.manage",
    )
    await require_channel_permissions(
        session,
        redis,
        target,
        auth.user,
        required_permissions("webhook.manage"),
    )
    if generation is None:
        if ordinary is None:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
        await lock_announcement_mutation(session)
        if source.guild is None or target.guild is None:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
        for guild_ref in sorted(
            {
                (source.guild.id, source.guild.origin_domain),
                (target.guild.id, target.guild.origin_domain),
            },
            key=lambda ref: (ref[1], ref[0]),
        ):
            await session.get(Guild, guild_ref, with_for_update=True)
        await session.refresh(source.channel)
        await session.refresh(target.channel)
        ordinary = await session.get(
            ChannelFollow,
            ordinary.id,
            with_for_update=True,
            populate_existing=True,
        )
        if ordinary is None or not ordinary.active:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
        await require_announcement_actor_scope(
            session,
            source,
            auth.user,
            application,
            "channels.read",
        )
        await require_announcement_actor_scope(
            session,
            target,
            auth.user,
            application,
            "webhooks.manage",
        )
        await require_channel_permissions(
            session,
            redis,
            source,
            auth.user,
            required_permissions("announcement.follow.source"),
        )
        await require_channel_permissions(
            session,
            redis,
            target,
            auth.user,
            required_permissions("webhook.manage"),
        )
        ordinary.active = False
        previous_avatar = await detach_announcement_follower_avatar(session, ordinary)
        await session.commit()
        if target.guild is not None:
            await publish_follower_webhook_update(redis, target.guild, target.channel)
        if previous_avatar is not None:
            await enqueue_best_effort(
                media_local_purge,
                previous_avatar.id,
                previous_avatar.origin_domain,
            )
        return Response(status_code=204)

    if federated is None:
        raise RuntimeError("federated announcement follow binding disappeared")
    actor_profile = profile_from_user(auth.user)
    target_authority = target.guild.origin_domain if target.guild is not None else target_ref[1]
    if target_authority == settings.domain:
        await revoke_federated_announcement_follow_target(
            session,
            redis,
            settings,
            auth.user,
            application,
            follow_id,
            generation,
        )
    else:
        revoked = await signed_request(
            session,
            settings,
            "POST",
            target_authority,
            f"/_kaede/v1/channels/{target_ref[0]}/announcement-follow-revoke",
            payload={
                "actor": actor_profile,
                "actor_application_ref": (
                    f"{application.id}@{application.origin_domain}"
                    if application is not None
                    else None
                ),
                "actor_intent": actor_intent_for_authority(
                    actor_intent,
                    actor_intents,
                    target_authority,
                ),
                "follow_id": str(follow_id),
                "generation": str(generation),
            },
            request_timeout=10,
            max_response_bytes=64 * 1024,
        )
        if revoked.status_code != 200:
            raise HTTPException(
                status_code=revoked.status_code,
                detail={"code": "ANNOUNCEMENT_FOLLOW_REVOCATION_FAILED"},
            )
        raw_receipt = decode_federation_response_json(revoked)
        try:
            _receipt, projection = await validated_announcement_follow_receipt(
                session,
                settings,
                raw_receipt,
                expected_type="guild.announcement.follow.revoked",
            )
            if (
                projection.follow_id != follow_id
                or projection.generation != generation + 1
                or projection.source_ref != (source.channel.id, source.channel.origin_domain)
                or projection.target_ref != target_ref
                or projection.creator_ref != (federated.creator_id, federated.creator_domain)
            ):
                raise ValueError("announcement follow revocation receipt is mismatched")
        except (FederationNetworkError, HTTPException, TypeError, ValueError):
            raise HTTPException(
                status_code=502,
                detail={"code": "ANNOUNCEMENT_FOLLOW_REVOCATION_INVALID"},
            ) from None
    return Response(status_code=204)


ANNOUNCEMENT_PUBLISH_LIMIT = 10
ANNOUNCEMENT_PUBLISH_WINDOW = timedelta(hours=1)
ANNOUNCEMENT_DELETED_CONTENT = "[Original Message Deleted]"


@dataclass(slots=True)
class AnnouncementSyncEffects:
    dispatches: list[tuple[ChannelAccess, dict[str, object]]] = field(default_factory=list)
    guilds: dict[tuple[int, str], Guild] = field(default_factory=dict)
    media_purge: list[Attachment] = field(default_factory=list)
    federation_destinations: set[str] = field(default_factory=set)

    def merge(self, other: AnnouncementSyncEffects) -> None:
        self.dispatches.extend(other.dispatches)
        self.guilds.update(other.guilds)
        self.media_purge.extend(other.media_purge)
        self.federation_destinations.update(other.federation_destinations)

    async def publish(self, redis: Redis) -> None:
        for guild in self.guilds.values():
            await wake_queued_guild_federation(guild)
        for access, payload in self.dispatches:
            await publish_channel_dispatch(redis, access, "MESSAGE_UPDATE", payload)
        for attachment in self.media_purge:
            await enqueue_best_effort(
                media_local_purge,
                attachment.id,
                attachment.origin_domain,
            )
        for destination in sorted(self.federation_destinations):
            await enqueue_best_effort(federation_deliver, destination)


def apply_announcement_copy_projection(
    destination: Message,
    source: Message,
    *,
    changed_at: datetime,
    source_deleted: bool,
    initial: bool = False,
) -> None:
    """Apply the immutable follower-copy projection without user-forward semantics."""

    # A delivered copy is generated by the target-owned type-2 follower
    # webhook. Preserve that local attribution snapshot across source edits;
    # copying a source webhook id into a target-owned message would let the
    # target guild falsely attest another authority's webhook identity.
    follower_webhook = (
        (
            destination.webhook_id,
            destination.webhook_domain,
            destination.webhook_name,
            destination.webhook_avatar_hash,
            destination.webhook_avatar_url,
        )
        if destination.webhook_id is not None
        and destination.webhook_domain == destination.origin_domain
        else None
    )
    destination.message_type = 0
    destination.forwarded_message_id = source.id
    destination.forwarded_message_domain = source.origin_domain
    destination.forwarded_channel_id = source.channel_id
    destination.forwarded_channel_domain = source.channel_domain
    destination.forward_snapshot = None
    destination.referenced_message_id = None
    destination.referenced_message_domain = None
    destination.mention_user_refs = []
    destination.mention_role_refs = []
    destination.mention_everyone = False
    destination.client_nonce = None
    destination.tts = False
    destination.deleted_at = None
    destination.edited_at = None if initial else changed_at
    copied_flags = int(source.flags or 0) & (
        MESSAGE_FLAG_SUPPRESS_EMBEDS | MESSAGE_FLAG_IS_COMPONENTS_V2
    )
    destination.flags = copied_flags | MESSAGE_FLAG_IS_CROSSPOST
    if source_deleted:
        destination.flags |= MESSAGE_FLAG_SOURCE_MESSAGE_DELETED
        destination.content = ANNOUNCEMENT_DELETED_CONTENT
        destination.e2ee = None
        destination.embeds = []
        destination.components = []
        destination.sticker_items = []
        destination.application_id = None
        destination.application_domain = None
        destination.view_version = 0
        destination.webhook_id = None
        destination.webhook_domain = None
        destination.webhook_name = None
        destination.webhook_avatar_hash = None
        destination.webhook_avatar_url = None
        if follower_webhook is not None:
            (
                destination.webhook_id,
                destination.webhook_domain,
                destination.webhook_name,
                destination.webhook_avatar_hash,
                destination.webhook_avatar_url,
            ) = follower_webhook
        return
    destination.content = source.content
    destination.e2ee = None
    destination.embeds = list(source.embeds or [])
    destination.components = list(source.components or [])
    destination.sticker_items = list(source.sticker_items or [])
    destination.application_id = source.application_id
    destination.application_domain = source.application_domain
    destination.view_version = int(source.view_version or 0)
    destination.webhook_id = source.webhook_id
    destination.webhook_domain = source.webhook_domain
    destination.webhook_name = source.webhook_name
    destination.webhook_avatar_hash = source.webhook_avatar_hash
    destination.webhook_avatar_url = source.webhook_avatar_url
    if follower_webhook is not None:
        (
            destination.webhook_id,
            destination.webhook_domain,
            destination.webhook_name,
            destination.webhook_avatar_hash,
            destination.webhook_avatar_url,
        ) = follower_webhook


def apply_announcement_follower_attribution(
    destination: Message,
    follow: ChannelFollow | FederatedChannelFollow,
    *,
    default_name: str,
) -> None:
    """Bind a copy to its target-owned Discord type-2 webhook identity."""

    if destination.origin_domain != follow.target_channel_domain or (
        destination.channel_id,
        destination.channel_domain,
    ) != (follow.target_channel_id, follow.target_channel_domain):
        raise RuntimeError("announcement follower attribution is outside its target")
    name = (follow.name or default_name).strip()[:80]
    if not name:
        name = "Channel Follower"
    destination.webhook_id = follow.id
    destination.webhook_domain = follow.target_channel_domain
    destination.webhook_name = name
    destination.webhook_avatar_hash = follow.avatar_hash
    destination.webhook_avatar_url = None


@dataclass(frozen=True, slots=True)
class AnnouncementCopyViewProjection:
    application_ref: tuple[int, str]
    integration_type: str
    installation_ref: tuple[int, str]
    installation_revision: int
    version: int
    persistent: bool
    expires_at: datetime | None


def announcement_copy_view_projection(
    source_view: MessageView | None,
) -> AnnouncementCopyViewProjection | None:
    if source_view is None:
        return None
    return AnnouncementCopyViewProjection(
        application_ref=(source_view.application_id, source_view.application_domain),
        integration_type=source_view.integration_type,
        installation_ref=(source_view.installation_id, source_view.installation_domain),
        installation_revision=source_view.installation_revision,
        version=source_view.version,
        persistent=source_view.persistent,
        expires_at=source_view.expires_at,
    )


async def target_announcement_copy_view_projection(
    session: AsyncSession,
    guild: Guild,
    source_view: AnnouncementCopyViewProjection | None,
) -> AnnouncementCopyViewProjection | None:
    """Rebind copied controls to an exact live installation at the target.

    Installation ids are guild-authority capabilities, not portable app ids.
    A copied source-guild lineage must therefore never be trusted in the
    follower guild. Link/display components remain useful without a binding;
    interactive controls become active only when the same application is
    installed in the target guild with its current revision.
    """

    if source_view is None:
        return None
    installation = await session.scalar(
        select(BotInstallation)
        .join(
            BotApplication,
            (BotApplication.id == BotInstallation.application_id)
            & (BotApplication.origin_domain == BotInstallation.application_domain),
        )
        .where(
            BotInstallation.application_id == source_view.application_ref[0],
            BotInstallation.application_domain == source_view.application_ref[1],
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            usable_guild_installation(),
            BotInstallation.granted_scopes.contains(["applications.commands"]),
            BotApplication.status == "active",
            BotApplication.bot_user_id == BotInstallation.bot_user_id,
            BotApplication.bot_user_domain == BotInstallation.bot_user_domain,
        )
    )
    if installation is None:
        return None
    return replace(
        source_view,
        integration_type="guild_install",
        installation_ref=(installation.id, installation.guild_domain),
        installation_revision=installation.grant_revision,
    )


async def sync_announcement_copy_view(
    session: AsyncSession,
    destination: Message,
    destination_view: MessageView | None,
    source_view: AnnouncementCopyViewProjection | None,
) -> MessageView | None:
    """Create, update, or remove a copied application component lifecycle."""

    if not destination.components or source_view is None:
        if destination_view is not None:
            await session.delete(destination_view)
        return None
    if (
        destination.application_id,
        destination.application_domain,
    ) != source_view.application_ref:
        raise RuntimeError("announcement copy view application binding changed")
    if destination_view is None:
        destination_view = MessageView(
            message_id=destination.id,
            message_domain=destination.origin_domain,
            application_id=source_view.application_ref[0],
            application_domain=source_view.application_ref[1],
            integration_type=source_view.integration_type,
            installation_id=source_view.installation_ref[0],
            installation_domain=source_view.installation_ref[1],
            installation_revision=source_view.installation_revision,
            version=source_view.version,
            persistent=source_view.persistent,
            expires_at=source_view.expires_at,
        )
        session.add(destination_view)
        return destination_view
    destination_view.application_id = source_view.application_ref[0]
    destination_view.application_domain = source_view.application_ref[1]
    destination_view.integration_type = source_view.integration_type
    destination_view.installation_id = source_view.installation_ref[0]
    destination_view.installation_domain = source_view.installation_ref[1]
    destination_view.installation_revision = source_view.installation_revision
    destination_view.version = source_view.version
    destination_view.persistent = source_view.persistent
    destination_view.expires_at = source_view.expires_at
    return destination_view


async def sync_target_announcement_copy_view(
    session: AsyncSession,
    guild: Guild,
    destination: Message,
    destination_view: MessageView | None,
    source_view: AnnouncementCopyViewProjection | None,
) -> MessageView | None:
    """Apply a target-authority installation binding or disable interactions."""

    target_view = await target_announcement_copy_view_projection(
        session,
        guild,
        source_view,
    )
    if destination.components and target_view is None:
        destination.application_id = None
        destination.application_domain = None
        destination.view_version = 0
    elif target_view is not None:
        destination.application_id = target_view.application_ref[0]
        destination.application_domain = target_view.application_ref[1]
        destination.view_version = target_view.version
    return await sync_announcement_copy_view(
        session,
        destination,
        destination_view,
        target_view,
    )


async def sync_local_announcement_attachments(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator | None,
    source: Message,
    destination: Message,
    *,
    source_deleted: bool,
    changed_at: datetime,
) -> tuple[list[Attachment], list[Attachment]]:
    """Retain local published media under copy-owned attachment identities."""

    source_attachments = (
        []
        if source_deleted
        else list(
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
    )
    if any(item.origin_domain != settings.domain for item in source_attachments):
        # A shared object key is safe only inside the owning object-store
        # authority. Cross-authority copies are materialized by the receiving
        # federation endpoint instead.
        raise HTTPException(
            status_code=409,
            detail={"code": "ANNOUNCEMENT_MEDIA_COPY_UNAVAILABLE"},
        )
    clones = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.message_id == destination.id,
                Attachment.message_domain == destination.origin_domain,
                Attachment.source_attachment_id.is_not(None),
            )
            .with_for_update()
        )
    )
    by_source = {
        (item.source_attachment_id, item.source_attachment_domain): item
        for item in clones
        if item.source_attachment_id is not None and item.source_attachment_domain is not None
    }
    retained: list[Attachment] = []
    retained_refs: set[tuple[int, str]] = set()
    for source_attachment in source_attachments:
        source_ref = (source_attachment.id, source_attachment.origin_domain)
        retained_refs.add(source_ref)
        clone = by_source.get(source_ref)
        if clone is None:
            if snowflake is None:
                raise RuntimeError("announcement attachment creation requires a snowflake")
            clone = Attachment(
                id=await snowflake.mint(),
                origin_domain=settings.domain,
                message_id=destination.id,
                message_domain=destination.origin_domain,
                source_attachment_id=source_attachment.id,
                source_attachment_domain=source_attachment.origin_domain,
                uploader_id=source_attachment.uploader_id,
                uploader_domain=source_attachment.uploader_domain,
                filename=source_attachment.filename,
                content_type=source_attachment.content_type,
                size=source_attachment.size,
                object_key=source_attachment.object_key,
                staging_object_key=None,
                width=source_attachment.width,
                height=source_attachment.height,
                duration_secs=source_attachment.duration_secs,
                waveform=source_attachment.waveform,
                blurhash=source_attachment.blurhash,
                content_sha256=source_attachment.content_sha256,
                perceptual_hash=source_attachment.perceptual_hash,
                detected_content_type=source_attachment.detected_content_type,
                variants=dict(source_attachment.variants or {}),
                media_transform=(
                    dict(source_attachment.media_transform)
                    if isinstance(source_attachment.media_transform, dict)
                    else None
                ),
                purpose="attachment",
                scan_status=source_attachment.scan_status,
                encryption_mode=source_attachment.encryption_mode,
                encryption_protocol=source_attachment.encryption_protocol,
                finalized_at=source_attachment.finalized_at or changed_at,
            )
            session.add(clone)
        else:
            clone.deleted_at = None
            clone.filename = source_attachment.filename
            clone.content_type = source_attachment.content_type
            clone.size = source_attachment.size
            clone.width = source_attachment.width
            clone.height = source_attachment.height
            clone.duration_secs = source_attachment.duration_secs
            clone.waveform = source_attachment.waveform
            clone.blurhash = source_attachment.blurhash
            clone.content_sha256 = source_attachment.content_sha256
            clone.perceptual_hash = source_attachment.perceptual_hash
            clone.detected_content_type = source_attachment.detected_content_type
            clone.variants = dict(source_attachment.variants or {})
            clone.scan_status = source_attachment.scan_status
        retained.append(clone)
    removed: list[Attachment] = []
    for clone in clones:
        clone_source_ref = (clone.source_attachment_id, clone.source_attachment_domain)
        if clone_source_ref not in retained_refs and clone.deleted_at is None:
            clone.deleted_at = changed_at
            removed.append(clone)
    await session.flush()
    return retained, removed


async def enforce_announcement_publish_limit(
    session: AsyncSession,
    source: Message,
    *,
    now: datetime,
) -> None:
    """Serialize at the channel lock and admit at most ten new publishes/hour."""

    if source.published_at is not None or int(source.flags or 0) & MESSAGE_FLAG_CROSSPOSTED:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MESSAGE_ALREADY_CROSSPOSTED",
                "message": "This message has already been crossposted.",
            },
        )
    cutoff = now - ANNOUNCEMENT_PUBLISH_WINDOW
    count, oldest = (
        await session.execute(
            select(func.count(Message.id), func.min(Message.published_at)).where(
                Message.channel_id == source.channel_id,
                Message.channel_domain == source.channel_domain,
                Message.published_at >= cutoff,
            )
        )
    ).one()
    if int(count or 0) >= ANNOUNCEMENT_PUBLISH_LIMIT:
        retry_after = max(
            1,
            int(((oldest + ANNOUNCEMENT_PUBLISH_WINDOW) - now).total_seconds()) + 1,
        )
        raise HTTPException(
            status_code=429,
            detail={
                "code": "ANNOUNCEMENT_PUBLISH_RATE_LIMITED",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )
    source.published_at = now


async def queue_announcement_crosspost_sync_event(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    signer: User,
    follow: FederatedChannelFollow,
    receipt: FederatedMessageCrosspost,
    source: Message,
    *,
    source_deleted: bool,
    source_author_profile: dict[str, object] | None,
    rendered_source: dict[str, object] | None,
) -> None:
    """Queue a copy mutation against the immutable delivery receipt grant."""

    envelope = await build_guild_authority_envelope(
        session,
        settings,
        guild,
        "announcement.crosspost.sync",
        signer,
        {
            "follow_id": str(follow.id),
            "generation": str(receipt.generation),
            "source_channel_ref": f"{source.channel_id}@{source.channel_domain}",
            "source_message_ref": f"{source.id}@{source.origin_domain}",
            "source_deleted": source_deleted,
            "source_author": source_author_profile,
            "message": rendered_source,
        },
        context={
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "channel_id": str(source.channel_id),
            "channel_domain": source.channel_domain,
        },
    )
    await queue_event(
        session,
        settings,
        follow.target_authority_domain,
        envelope,
    )


async def propagate_announcement_source_change(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator | None,
    guild: Guild,
    source: Message,
    actor: User,
    *,
    source_deleted: bool,
    changed_at: datetime,
) -> AnnouncementSyncEffects:
    """Update every follower copy and durably queue cross-authority convergence."""

    effects = AnnouncementSyncEffects()
    # Edit/delete callers take this fence before their source-guild lock. It is
    # re-entrant here and freezes retained (including unfollowed) copy receipts
    # while all destination guild rows are locked before message rows.
    await lock_announcement_mutation(session)
    local_target_channels = list(
        await session.scalars(
            select(Channel)
            .join(
                ChannelFollow,
                (ChannelFollow.target_channel_id == Channel.id)
                & (ChannelFollow.target_channel_domain == Channel.origin_domain),
            )
            .join(MessageCrosspost, MessageCrosspost.follow_id == ChannelFollow.id)
            .where(
                MessageCrosspost.source_message_id == source.id,
                MessageCrosspost.source_message_domain == source.origin_domain,
            )
        )
    )
    await lock_announcement_guilds(
        session,
        {
            (channel.guild_id, channel.guild_domain)
            for channel in local_target_channels
            if channel.guild_id is not None and channel.guild_domain == settings.domain
        },
    )
    local_receipts = list(
        await session.scalars(
            select(MessageCrosspost)
            .where(
                MessageCrosspost.source_message_id == source.id,
                MessageCrosspost.source_message_domain == source.origin_domain,
            )
            .with_for_update()
        )
    )
    source_author = await session.get(User, (source.author_id, source.author_domain))
    if (local_receipts or not source_deleted) and source_author is None:
        raise RuntimeError("announcement source author disappeared")
    source_view = await session.get(MessageView, (source.id, source.origin_domain))
    for local_receipt in local_receipts:
        local_follow = await session.get(
            ChannelFollow,
            local_receipt.follow_id,
            with_for_update=True,
            populate_existing=True,
        )
        destination = await session.get(
            Message,
            (
                local_receipt.destination_message_id,
                local_receipt.destination_message_domain,
            ),
            with_for_update=True,
        )
        if local_follow is None or destination is None or destination.deleted_at is not None:
            continue
        target = await session.scalar(
            select(Channel)
            .where(
                Channel.id == local_follow.target_channel_id,
                Channel.origin_domain == local_follow.target_channel_domain,
            )
            .execution_options(populate_existing=True)
        )
        if target is None or target.unavailable:
            continue
        target_guild = await session.get(Guild, (target.guild_id, target.guild_domain))
        if (
            target_guild is None
            or target_guild.origin_domain != settings.domain
            or target_guild.unavailable
            or target.type != 0
            or (target.guild_id, target.guild_domain)
            != (target_guild.id, target_guild.origin_domain)
            or not source_deleted
            and (target.encryption_mode == "e2ee" or target.e2ee_required)
        ):
            continue
        apply_announcement_copy_projection(
            destination,
            source,
            changed_at=changed_at,
            source_deleted=source_deleted,
        )
        destination_attachments, removed = await sync_local_announcement_attachments(
            session,
            settings,
            snowflake,
            source,
            destination,
            source_deleted=source_deleted,
            changed_at=changed_at,
        )
        effects.media_purge.extend(removed)
        destination_view = await session.get(
            MessageView,
            (destination.id, destination.origin_domain),
            with_for_update=True,
        )
        destination_view = await sync_target_announcement_copy_view(
            session,
            target_guild,
            destination,
            destination_view,
            (None if source_deleted else announcement_copy_view_projection(source_view)),
        )
        projection = await session.get(
            MessageProjection,
            (destination.id, destination.origin_domain),
            with_for_update=True,
        )
        if projection is not None:
            projection.mention_user_refs = []
        rendered = message_payload(
            destination,
            source_author,
            destination_attachments,
            view=destination_view,
        )
        target_access = ChannelAccess(channel=target, guild=target_guild, participants=[])
        crosspost_signer = await guild_authority_owner(session, settings, target_guild)
        await queue_guild_mutation(
            session,
            settings,
            target_guild,
            crosspost_signer,
            "guild.message.update",
            {"message": rendered, "announcement_copy_updated": True},
            channel=target,
        )
        effects.guilds[(target_guild.id, target_guild.origin_domain)] = target_guild
        effects.dispatches.append((target_access, rendered))

    federated_receipts = list(
        await session.scalars(
            select(FederatedMessageCrosspost)
            .where(
                FederatedMessageCrosspost.source_message_id == source.id,
                FederatedMessageCrosspost.source_message_domain == source.origin_domain,
                FederatedMessageCrosspost.local_role == "source",
            )
            .with_for_update()
        )
    )
    if federated_receipts:
        signer = await guild_authority_owner(session, settings, guild)
        rendered_source = (
            None
            if source_deleted
            else await render_message_payload(
                session,
                source,
                source_author,
                include_forward_source=True,
            )
        )
        source_author_profile = (
            profile_from_user(source_author) if source_author is not None else None
        )
        for federated_receipt in federated_receipts:
            if not source_deleted and rendered_source is not None:
                federated_receipt.source_projection = rendered_source
                federated_receipt.source_author_profile = source_author_profile
            if federated_receipt.delivery_status != "delivered":
                continue
            federated_follow = await session.get(
                FederatedChannelFollow,
                federated_follow_key(
                    federated_receipt.follow_id,
                    federated_receipt.follow_authority_domain,
                    "source",
                ),
            )
            if federated_follow is None:
                continue
            await queue_announcement_crosspost_sync_event(
                session,
                settings,
                guild,
                signer,
                federated_follow,
                federated_receipt,
                source,
                source_deleted=source_deleted,
                source_author_profile=source_author_profile,
                rendered_source=rendered_source,
            )
            effects.federation_destinations.add(federated_follow.target_authority_domain)
    return effects


def _announcement_delivery_retry_at(now: datetime, attempts: int) -> datetime:
    return now + timedelta(seconds=min(3_600, 5 * (2 ** min(attempts, 10))))


async def deliver_federated_announcement_crosspost_job(
    session: AsyncSession,
    settings: Settings,
    *,
    source_message_id: int,
    source_message_domain: str,
    follow_id: int,
    follow_authority_domain: str,
    now: datetime | None = None,
) -> tuple[str, str | None]:
    """Deliver one persisted follower copy without holding up the publisher."""

    current = now or datetime.now(UTC)
    lock_key = (
        "announcement-crosspost-delivery:"
        f"{source_message_id}@{source_message_domain}:"
        f"{qualified_follow_ref(follow_id, follow_authority_domain)}"
    )
    await session.scalar(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))
    receipt = await session.get(
        FederatedMessageCrosspost,
        federated_crosspost_key(
            source_message_id,
            source_message_domain,
            follow_id,
            follow_authority_domain,
            "source",
        ),
        with_for_update=True,
    )
    if receipt is None:
        return "missing", None
    if receipt.delivery_status in {"delivered", "terminal"}:
        return receipt.delivery_status, None
    if receipt.next_retry_at > current:
        return "deferred", None

    receipt.attempts += 1
    follow = await session.get(
        FederatedChannelFollow,
        federated_follow_key(follow_id, follow_authority_domain, "source"),
    )
    source = await session.get(Message, (source_message_id, source_message_domain))
    if (
        follow is None
        or source is None
        or source.e2ee is not None
        or source.published_at is None
        or (source.channel_id, source.channel_domain)
        != (follow.source_channel_id, follow.source_channel_domain)
        or receipt.generation < 1
        or receipt.published_at > current + timedelta(minutes=1)
    ):
        receipt.delivery_status = "terminal"
        receipt.last_error = "ANNOUNCEMENT_CROSSPOST_SOURCE_UNAVAILABLE"
        await session.commit()
        return "terminal", None
    author = await session.get(User, (source.author_id, source.author_domain))
    stored_projection = receipt.source_projection
    stored_author_profile = receipt.source_author_profile
    if not isinstance(stored_projection, dict) or not isinstance(stored_author_profile, dict):
        receipt.delivery_status = "terminal"
        receipt.last_error = "ANNOUNCEMENT_CROSSPOST_SNAPSHOT_UNAVAILABLE"
        await session.commit()
        return "terminal", None

    if source.deleted_at is None:
        if author is None:
            receipt.delivery_status = "terminal"
            receipt.last_error = "ANNOUNCEMENT_CROSSPOST_AUTHOR_UNAVAILABLE"
            await session.commit()
            return "terminal", None
        source_projection = await render_message_payload(
            session,
            source,
            author,
            include_forward_source=True,
        )
        source_author_profile = profile_from_user(author)
        receipt.source_projection = source_projection
        receipt.source_author_profile = source_author_profile
    else:
        source_projection = stored_projection
        source_author_profile = stored_author_profile
    raw_application_id = source_projection.get("application_id")
    raw_application_domain = source_projection.get("application_domain")
    application_ref = (
        f"{raw_application_id}@{raw_application_domain}"
        if raw_application_id is not None and raw_application_domain is not None
        else None
    )
    try:
        upstream = await signed_request(
            session,
            settings,
            "POST",
            follow.target_authority_domain,
            (f"/_kaede/v1/channels/{follow.target_channel_id}/announcement-crossposts"),
            payload={
                "follow_id": str(follow.id),
                "generation": str(receipt.generation),
                "source_channel_ref": (
                    f"{follow.source_channel_id}@{follow.source_channel_domain}"
                ),
                "source_message_ref": f"{source.id}@{source.origin_domain}",
                "source_author": source_author_profile,
                "source_message": source_projection,
                "application_ref": application_ref,
                "published_at": receipt.published_at.isoformat(),
            },
            request_timeout=15,
            max_response_bytes=512 * 1024,
        )
    except FederationNetworkError as exc:
        if receipt.attempts < 12:
            receipt.delivery_status = "retry"
            receipt.next_retry_at = _announcement_delivery_retry_at(
                current,
                receipt.attempts,
            )
        else:
            receipt.delivery_status = "terminal"
        receipt.last_error = type(exc).__name__[:500]
        await session.commit()
        return receipt.delivery_status, None

    if upstream.status_code != 201:
        raw_error = decode_federation_response_json(upstream)
        code = "ANNOUNCEMENT_CROSSPOST_DELIVERY_FAILED"
        if isinstance(raw_error, dict) and isinstance(raw_error.get("detail"), dict):
            supplied_code = raw_error["detail"].get("code")
            if isinstance(supplied_code, str):
                code = supplied_code[:500]
        retryable = upstream.status_code in {408, 409, 425, 429} or upstream.status_code >= 500
        if retryable and receipt.attempts < 12:
            receipt.delivery_status = "retry"
            receipt.next_retry_at = _announcement_delivery_retry_at(
                current,
                receipt.attempts,
            )
        else:
            receipt.delivery_status = "terminal"
        receipt.last_error = code
        await session.commit()
        return receipt.delivery_status, None

    raw_delivery = decode_federation_response_json(upstream)
    try:
        if not isinstance(raw_delivery, dict) or set(raw_delivery) != {
            "destination_message_ref",
            "message",
        }:
            raise ValueError("delivery response is not an object")
        raw_destination_ref = raw_delivery["destination_message_ref"]
        rendered_destination = raw_delivery["message"]
        if not isinstance(raw_destination_ref, str) or "@" not in raw_destination_ref:
            raise ValueError("delivery destination reference is invalid")
        destination_ref = EntityRef(raw_destination_ref).resolve(settings.domain)
        if (
            raw_destination_ref != f"{destination_ref[0]}@{destination_ref[1]}"
            or destination_ref[1] != follow.target_authority_domain
            or not isinstance(rendered_destination, dict)
            or rendered_destination.get("id") != str(destination_ref[0])
            or rendered_destination.get("origin_domain") != destination_ref[1]
            or rendered_destination.get("channel_id") != str(follow.target_channel_id)
            or rendered_destination.get("channel_domain") != follow.target_channel_domain
            or rendered_destination.get("forwarded_message_id") != str(source.id)
            or rendered_destination.get("forwarded_message_domain") != source.origin_domain
            or rendered_destination.get("forwarded_channel_id") != str(follow.source_channel_id)
            or rendered_destination.get("forwarded_channel_domain") != follow.source_channel_domain
        ):
            raise ValueError("delivery destination authority mismatch")
    except (KeyError, TypeError, ValueError):
        if receipt.attempts < 12:
            receipt.delivery_status = "retry"
            receipt.next_retry_at = _announcement_delivery_retry_at(
                current,
                receipt.attempts,
            )
        else:
            receipt.delivery_status = "terminal"
        receipt.last_error = "ANNOUNCEMENT_CROSSPOST_DELIVERY_INVALID"
        await session.commit()
        return receipt.delivery_status, None

    receipt.destination_message_id = destination_ref[0]
    receipt.destination_message_domain = destination_ref[1]
    receipt.delivery_status = "delivered"
    receipt.last_error = None
    delivery_wake: str | None = None
    if source.deleted_at is not None:
        source_channel = await session.get(Channel, (source.channel_id, source.channel_domain))
        source_guild = (
            await session.get(
                Guild,
                (source_channel.guild_id, source_channel.guild_domain),
            )
            if source_channel is not None and source_channel.guild_id is not None
            else None
        )
        if source_guild is not None:
            signer = await guild_authority_owner(session, settings, source_guild)
            await queue_announcement_crosspost_sync_event(
                session,
                settings,
                source_guild,
                signer,
                follow,
                receipt,
                source,
                source_deleted=True,
                source_author_profile=None,
                rendered_source=None,
            )
            delivery_wake = follow.target_authority_domain
    await session.commit()
    return "delivered", delivery_wake


@router.post("/{channel_id}/messages/{message_id}/crosspost")
async def crosspost_message(
    channel_id: EntityRef,
    message_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        result = await proxy_remote_guild_message_operation(
            session,
            settings,
            access,
            auth.user,
            "announcement.crosspost",
            message_ref=message_id,
        )
        rendered = result.get("message")
        if not isinstance(rendered, dict):
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
            )
        return {str(key): value for key, value in rendered.items()}
    require_local_mutation_authority(access, settings)
    if access.guild is None or access.channel.type != 5:
        raise HTTPException(status_code=400, detail={"code": "ANNOUNCEMENT_CHANNEL_REQUIRED"})
    access = await lock_announcement_publish_mutation(session, settings, access)
    guild = access.guild
    if guild is None or access.channel.type != 5 or access.channel.encryption_mode == "e2ee":
        raise HTTPException(status_code=409, detail={"code": "ANNOUNCEMENT_SOURCE_CHANGED"})
    await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("message.create"),
    )
    source = await channel_message(
        session, settings, access.channel, message_id, for_update=True, require_active=True
    )
    if (source.author_id, source.author_domain) != (
        auth.user.id,
        auth.user.origin_domain,
    ):
        await require_channel_permissions(
            session,
            redis,
            access,
            auth.user,
            required_permissions("message.delete.other"),
        )
    if source.e2ee is not None or access.channel.encryption_mode == "e2ee":
        raise HTTPException(status_code=409, detail={"code": "E2EE_CROSSPOST_UNSUPPORTED"})
    if (
        source.message_type not in {0, 19, 20, 23}
        or await session.get(Poll, (source.id, source.origin_domain)) is not None
        or source.forward_snapshot is not None
        or int(source.flags or 0) & MESSAGE_FLAG_HAS_SNAPSHOT
        or source.forwarded_message_id is not None
        or source.forwarded_message_domain is not None
        or source.forwarded_channel_id is not None
        or source.forwarded_channel_domain is not None
    ):
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_NOT_CROSSPOSTABLE"})
    published_at = datetime.now(UTC)
    await enforce_announcement_publish_limit(session, source, now=published_at)
    follows = list(
        await session.scalars(
            select(ChannelFollow)
            .where(
                ChannelFollow.source_channel_id == access.channel.id,
                ChannelFollow.source_channel_domain == access.channel.origin_domain,
                ChannelFollow.active.is_(True),
            )
            .with_for_update()
        )
    )
    federated_follows = list(
        await session.scalars(
            select(FederatedChannelFollow)
            .where(
                FederatedChannelFollow.source_channel_id == access.channel.id,
                FederatedChannelFollow.source_channel_domain == access.channel.origin_domain,
                FederatedChannelFollow.local_role == "source",
                FederatedChannelFollow.active.is_(True),
            )
            .with_for_update()
        )
    )
    existing_follow_ids = set(
        await session.scalars(
            select(MessageCrosspost.follow_id).where(
                MessageCrosspost.source_message_id == source.id,
                MessageCrosspost.source_message_domain == source.origin_domain,
            )
        )
    )
    existing_federated_follow_refs = set(
        (
            await session.execute(
                select(
                    FederatedMessageCrosspost.follow_id,
                    FederatedMessageCrosspost.follow_authority_domain,
                ).where(
                    FederatedMessageCrosspost.source_message_id == source.id,
                    FederatedMessageCrosspost.source_message_domain == source.origin_domain,
                    FederatedMessageCrosspost.local_role == "source",
                )
            )
        ).tuples()
    )
    published: list[tuple[ChannelAccess, Message, dict[str, object]]] = []
    target_guilds: dict[tuple[int, str], Guild] = {}
    author = await session.get(User, (source.author_id, source.author_domain))
    if author is None:
        raise RuntimeError("announcement source author disappeared")
    source_projection = await render_message_payload(
        session,
        source,
        author,
        include_forward_source=True,
    )
    source_author_profile = profile_from_user(author)
    source_view = await session.get(MessageView, (source.id, source.origin_domain))
    federated_delivery_jobs: list[tuple[int, str, int, str]] = []
    for follow in follows:
        if follow.id in existing_follow_ids:
            continue
        target = await session.scalar(
            select(Channel)
            .where(
                Channel.id == follow.target_channel_id,
                Channel.origin_domain == follow.target_channel_domain,
            )
            .execution_options(populate_existing=True)
        )
        if target is None or target.unavailable:
            follow.active = False
            continue
        target_guild = await session.get(Guild, (target.guild_id, target.guild_domain))
        if (
            target_guild is None
            or target_guild.origin_domain != settings.domain
            or target_guild.unavailable
            or (target.guild_id, target.guild_domain)
            != (target_guild.id, target_guild.origin_domain)
        ):
            follow.active = False
            continue
        if target.type != 0 or target.encryption_mode == "e2ee" or target.e2ee_required:
            follow.active = False
            continue
        target_access = ChannelAccess(channel=target, guild=target_guild, participants=[])
        destination = Message(
            id=await snowflake.mint(),
            origin_domain=settings.domain,
            channel_id=target.id,
            channel_domain=target.origin_domain,
            author_id=source.author_id,
            author_domain=source.author_domain,
            content=source.content,
            e2ee=None,
            embeds=list(source.embeds or []),
            components=list(source.components or []),
            sticker_items=list(source.sticker_items or []),
            encryption_policy_generation=target.encryption_policy_generation,
            encryption_epoch=target.encryption_epoch,
            message_type=0,
            flags=MESSAGE_FLAG_IS_CROSSPOST,
            forwarded_message_id=source.id,
            forwarded_message_domain=source.origin_domain,
            forwarded_channel_id=source.channel_id,
            forwarded_channel_domain=source.channel_domain,
            application_id=source.application_id,
            application_domain=source.application_domain,
            view_version=int(source.view_version or 0),
        )
        apply_announcement_copy_projection(
            destination,
            source,
            changed_at=published_at,
            source_deleted=False,
            initial=True,
        )
        apply_announcement_follower_attribution(
            destination,
            follow,
            default_name=guild.name,
        )
        session.add(destination)
        await session.flush()
        (
            destination_attachments,
            _removed_copy_attachments,
        ) = await sync_local_announcement_attachments(
            session,
            settings,
            snowflake,
            source,
            destination,
            source_deleted=False,
            changed_at=published_at,
        )
        destination_view = await sync_target_announcement_copy_view(
            session,
            target_guild,
            destination,
            None,
            announcement_copy_view_projection(source_view),
        )
        session.add(
            MessageCrosspost(
                source_message_id=source.id,
                source_message_domain=source.origin_domain,
                follow_id=follow.id,
                destination_message_id=destination.id,
                destination_message_domain=destination.origin_domain,
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
        rendered_destination = message_payload(
            destination,
            author,
            destination_attachments,
            view=destination_view,
        )
        crosspost_signer = await guild_authority_owner(session, settings, target_guild)
        await queue_guild_mutation(
            session,
            settings,
            target_guild,
            crosspost_signer,
            "guild.message.create",
            {
                "message": rendered_destination,
                "author": profile_from_user(author),
                "thread_starter": False,
            },
            channel=target,
        )
        target_guilds[(target_guild.id, target_guild.origin_domain)] = target_guild
        published.append((target_access, destination, rendered_destination))
    for federated_follow in federated_follows:
        follow_ref = (
            federated_follow.id,
            federated_follow.target_authority_domain,
        )
        if follow_ref in existing_federated_follow_refs:
            continue
        session.add(
            FederatedMessageCrosspost(
                source_message_id=source.id,
                source_message_domain=source.origin_domain,
                follow_id=federated_follow.id,
                follow_authority_domain=federated_follow.target_authority_domain,
                local_role="source",
                generation=federated_follow.generation,
                destination_message_id=None,
                destination_message_domain=None,
                delivery_status="pending",
                attempts=0,
                next_retry_at=published_at,
                source_projection=source_projection,
                source_author_profile=source_author_profile,
                published_at=published_at,
            )
        )
        federated_delivery_jobs.append(
            (
                source.id,
                source.origin_domain,
                federated_follow.id,
                federated_follow.target_authority_domain,
            )
        )
    source.flags |= MESSAGE_FLAG_CROSSPOSTED
    source.published_at = source.published_at or published_at
    await mark_guild_activity(session, settings, access, auth.user)
    rendered_source = await render_message_payload(session, source, author, viewer=auth.user)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.message.update",
        {"message": rendered_source, "announcement_published": True},
        channel=access.channel,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    for target_guild in target_guilds.values():
        await wake_queued_guild_federation(target_guild)
    for target_access, _, rendered in published:
        await publish_channel_dispatch(redis, target_access, "MESSAGE_CREATE", rendered)
    await publish_channel_dispatch(redis, access, "MESSAGE_UPDATE", rendered_source)
    for source_id, source_domain, follow_id, follow_authority_domain in federated_delivery_jobs:
        await enqueue_best_effort(
            announcement_crosspost_deliver,
            source_id,
            source_domain,
            follow_id,
            follow_authority_domain,
        )
    return rendered_source


def require_pinnable_channel(access: ChannelAccess) -> None:
    """Keep Discord guild pins in text/post contexts; DMs remain saved pins."""

    if access.guild is not None and not is_pinnable_guild_channel_type(access.channel.type):
        raise HTTPException(
            status_code=400,
            detail={"code": "PINS_UNSUPPORTED_CHANNEL"},
        )


async def record_pin_audit_entry(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    access: ChannelAccess,
    actor: User,
    message: Message,
    *,
    pinned: bool,
    reason: str | None = None,
) -> None:
    """Record Discord's guild message-pin audit action; DMs have no audit log."""

    if access.guild is None:
        return
    await add_audit_entry(
        session,
        snowflake,
        access.guild,
        actor,
        74 if pinned else 75,
        target_type="message",
        target_ref={
            "id": str(message.id),
            "origin_domain": message.origin_domain,
            "channel_id": str(access.channel.id),
            "channel_domain": access.channel.origin_domain,
        },
        reason=normalize_audit_reason(reason),
    )


async def persist_pin_notice(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    access: ChannelAccess,
    actor: User,
    pinned_message: Message,
) -> tuple[Message, dict[str, object], set[str]]:
    """Persist and federate Discord's type-6 pin system message."""

    channel = access.channel
    message = Message(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=actor.id,
        author_domain=actor.origin_domain,
        content=None,
        e2ee=None,
        encryption_policy_generation=channel.encryption_policy_generation,
        encryption_epoch=channel.encryption_epoch,
        message_type=PIN_NOTICE_MESSAGE_TYPE,
        flags=0,
        message_reference=build_qualified_message_reference(
            message_type=PIN_NOTICE_MESSAGE_TYPE,
            message_ref=(pinned_message.id, pinned_message.origin_domain),
            channel_ref=(channel.id, channel.origin_domain),
            guild_ref=(
                (access.guild.id, access.guild.origin_domain) if access.guild is not None else None
            ),
        ),
        referenced_message_id=pinned_message.id,
        referenced_message_domain=pinned_message.origin_domain,
        mention_user_refs=[],
        mention_role_refs=[],
        mention_everyone=False,
    )
    destinations: set[str] = set()
    if access.guild is None:
        conversation = await session.get(
            DMConversation,
            (channel.id, channel.origin_domain),
        )
        if (
            conversation is None
            or conversation.origin_domain != settings.domain
            or conversation.authority_domain != settings.domain
        ):
            raise RuntimeError("pin notice must be created by the DM authority")
        await admit_federated_dm_message(
            session,
            settings,
            conversation,
            message_id=message.id,
            message_domain=message.origin_domain,
            delta=dm_message_storage_delta(
                content=None,
                e2ee=None,
                mention_user_refs=[],
                attachments=[],
            ),
            protected_refs={(pinned_message.id, pinned_message.origin_domain)},
        )
    session.add(message)
    await session.flush()
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            mention_user_refs=[],
        )
    )
    if channel.type in THREAD_CHANNEL_TYPES:
        advance_thread_message_projection(channel, message)
        channel.message_count = int(channel.message_count or 0) + 1
        channel.total_message_sent = int(channel.total_message_sent or 0) + 1
    elif channel.last_message_id is None or (
        channel.last_message_id,
        channel.last_message_domain or "",
    ) < (message.id, message.origin_domain):
        channel.last_message_id = message.id
        channel.last_message_domain = message.origin_domain
    rendered = message_payload(message, actor, [])
    if access.guild is not None:
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            actor,
            "guild.message.create",
            {
                "message": rendered,
                "author": profile_from_user(actor),
                "thread_starter": False,
            },
            channel=channel,
        )
    else:
        conversation = await session.get(
            DMConversation,
            (channel.id, channel.origin_domain),
        )
        if conversation is None:
            raise RuntimeError("pin notice conversation disappeared")
        destinations = {
            participant.origin_domain
            for participant in access.participants
            if participant.origin_domain != settings.domain
        }
        event_type = (
            "dm.group.message.committed" if conversation.type == "group" else "dm.message.create"
        )
        envelope = await build_envelope(
            session,
            settings,
            event_type,
            actor,
            {"message": rendered, "author": profile_from_user(actor)},
            context=(
                {
                    "conversation_id": str(conversation.id),
                    "conversation_domain": conversation.origin_domain,
                    "state_version": str(conversation.state_version),
                }
                if conversation.type == "group"
                else None
            ),
            authority_attested_actor=actor.origin_domain != settings.domain,
        )
        for destination in destinations:
            await queue_event(session, settings, destination, envelope)
    return message, rendered, destinations


@router.put("/{channel_id}/pins/{message_id}", status_code=204)
@router.put("/{channel_id}/messages/pins/{message_id}", status_code=204)
async def pin_message(
    channel_id: EntityRef,
    message_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    require_pinnable_channel(access)
    require_unarchived_thread(access.channel)
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("pin.update")
        )
        return await proxy_remote_guild_pin(
            session,
            settings,
            access,
            auth.user,
            message_id,
            pinned=True,
            reason=reason,
        )
    if access.guild is None and access.channel.origin_domain != settings.domain:
        await proxy_remote_dm_message_operation(
            session,
            settings,
            access,
            auth.user,
            "pin.add",
            message_id,
        )
        return Response(status_code=204)
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    require_unarchived_thread(channel)
    if access.guild is not None:
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("pin.update")
        )
    else:
        # A DM pin belongs to the conversation and either participant may
        # maintain it. Membership was already established by load_channel_access.
        await require_dm_send(session, access, auth.user)
    message = await channel_message(
        session,
        settings,
        channel,
        message_id,
        for_update=True,
        require_active=True,
    )
    if not message_is_pinnable(message):
        raise HTTPException(status_code=400, detail={"code": "SYSTEM_MESSAGE_NOT_PINNABLE"})
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
            pinned_by_id=auth.user.id,
            pinned_by_domain=auth.user.origin_domain,
        )
        .on_conflict_do_nothing()
        .returning(Pin.message_id)
    )
    federation_destinations: set[str] = set()
    pin_notice: dict[str, object] | None = None
    if inserted is not None and access.guild is not None:
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            auth.user,
            "guild.pin.add",
            {
                "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                "channel": {"id": str(channel.id), "origin_domain": channel.origin_domain},
                "user": {
                    "id": str(auth.user.id),
                    "origin_domain": auth.user.origin_domain,
                },
            },
            channel=channel,
        )
    elif inserted is not None:
        federation_destinations = await queue_dm_authority_mutation(
            session,
            settings,
            access,
            auth.user,
            "dm.pin.add",
            dm_message_mutation_content(access, message, auth.user),
        )
    if inserted is not None:
        _notice_message, pin_notice, notice_destinations = await persist_pin_notice(
            session,
            settings,
            snowflake,
            access,
            auth.user,
            message,
        )
        federation_destinations.update(notice_destinations)
        await record_pin_audit_entry(
            session,
            snowflake,
            access,
            auth.user,
            message,
            pinned=True,
            reason=reason,
        )
    pins_update = (
        await channel_pins_update_payload(
            session,
            channel,
            access.guild,
            changed_message=message,
            pinned=True,
        )
        if inserted is not None
        else None
    )
    await session.commit()
    if inserted is not None and access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    for destination in federation_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    if pins_update is not None:
        await publish_channel_dispatch(
            redis,
            access,
            "CHANNEL_PINS_UPDATE",
            pins_update,
        )
    if pin_notice is not None:
        await publish_channel_dispatch(redis, access, "MESSAGE_CREATE", pin_notice)
    return Response(status_code=204)


@router.get("/{channel_id}/pins")
async def list_pins(
    channel_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    """Deprecated Discord-compatible first-page projection."""

    page = await list_channel_pins(
        channel_id,
        before=None,
        limit=50,
        auth=auth,
        session=session,
        redis=redis,
        settings=settings,
    )
    return [
        {
            **cast(dict[str, object], item["message"]),
            "pinned_at": item["pinned_at"],
        }
        for item in cast(list[dict[str, object]], page["items"])
    ]


@router.get("/{channel_id}/messages/pins")
async def list_channel_pins(
    channel_id: EntityRef,
    before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=50),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return a newest-first Discord-style page of channel pins."""

    access = await load_channel_access(session, settings, auth.user, channel_id)
    require_pinnable_channel(access)
    if access.guild is not None:
        actor_permissions = await require_channel_permissions(
            session,
            redis,
            access,
            auth.user,
            required_permissions("pin.list"),
        )
        if not actor_permissions & Permission.READ_MESSAGE_HISTORY:
            return {"items": [], "has_more": False}
    try:
        cursor = normalize_pin_cursor(before)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "PIN_CURSOR_INVALID"}) from exc
    if (access.guild is not None and access.guild.origin_domain != settings.domain) or (
        access.guild is None and access.channel.origin_domain != settings.domain
    ):
        return await proxy_remote_channel_pins(
            session,
            settings,
            access,
            auth.user,
            before=cursor,
            limit=limit,
        )
    conditions = [
        Pin.channel_id == access.channel.id,
        Pin.channel_domain == access.channel.origin_domain,
    ]
    if cursor is not None:
        conditions.append(Pin.pinned_at < cursor)
    candidates = (
        await session.execute(
            select(Pin, Message, User)
            .join(
                Message,
                (Message.id == Pin.message_id) & (Message.origin_domain == Pin.message_domain),
            )
            .join(
                User,
                (User.id == Message.author_id) & (User.origin_domain == Message.author_domain),
            )
            .where(*conditions)
            .order_by(Pin.pinned_at.desc(), Pin.message_id.desc())
            .limit(limit + 1)
        )
    ).all()
    has_more = len(candidates) > limit
    rows = candidates[:limit]
    reaction_payloads = await reaction_payloads_for_messages(
        session,
        {(message.id, message.origin_domain) for _, message, _ in rows},
        viewer=auth.user,
    )
    items: list[dict[str, object]] = []
    for pin, message, author in rows:
        rendered = await render_message_payload(
            session,
            message,
            author,
            viewer=auth.user,
        )
        reactions = reaction_payloads.get((message.id, message.origin_domain), ({}, []))
        rendered.update(
            {
                "reaction_counts": reactions[0],
                "reacted_emoji": reactions[1],
                "pinned": True,
            }
        )
        items.append({"pinned_at": pin.pinned_at.isoformat(), "message": rendered})
    return {"items": items, "has_more": has_more}


@router.delete("/{channel_id}/pins/{message_id}", status_code=204)
@router.delete("/{channel_id}/messages/pins/{message_id}", status_code=204)
async def unpin_message(
    channel_id: EntityRef,
    message_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    require_pinnable_channel(access)
    require_unarchived_thread(access.channel)
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("pin.update")
        )
        return await proxy_remote_guild_pin(
            session,
            settings,
            access,
            auth.user,
            message_id,
            pinned=False,
            reason=reason,
        )
    if access.guild is None and access.channel.origin_domain != settings.domain:
        await proxy_remote_dm_message_operation(
            session,
            settings,
            access,
            auth.user,
            "pin.remove",
            message_id,
        )
        return Response(status_code=204)
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    require_unarchived_thread(access.channel)
    if access.guild is not None:
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("pin.update")
        )
    else:
        await require_dm_send(session, access, auth.user)
    message = await channel_message(session, settings, access.channel, message_id)
    removed = await session.scalar(
        delete(Pin)
        .where(
            Pin.channel_id == access.channel.id,
            Pin.channel_domain == access.channel.origin_domain,
            Pin.message_id == message.id,
            Pin.message_domain == message.origin_domain,
        )
        .returning(Pin.message_id)
    )
    federation_destinations: set[str] = set()
    if removed is not None and access.guild is not None:
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            auth.user,
            "guild.pin.remove",
            {
                "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                "channel": {
                    "id": str(access.channel.id),
                    "origin_domain": access.channel.origin_domain,
                },
                "user": {
                    "id": str(auth.user.id),
                    "origin_domain": auth.user.origin_domain,
                },
            },
            channel=access.channel,
        )
    elif removed is not None:
        federation_destinations = await queue_dm_authority_mutation(
            session,
            settings,
            access,
            auth.user,
            "dm.pin.remove",
            dm_message_mutation_content(access, message, auth.user),
        )
    if removed is not None:
        await record_pin_audit_entry(
            session,
            snowflake,
            access,
            auth.user,
            message,
            pinned=False,
            reason=reason,
        )
    pins_update = (
        await channel_pins_update_payload(
            session,
            access.channel,
            access.guild,
            changed_message=message,
            pinned=False,
        )
        if removed is not None
        else None
    )
    await session.commit()
    if removed is not None and access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    for destination in federation_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    if pins_update is not None:
        await publish_channel_dispatch(
            redis,
            access,
            "CHANNEL_PINS_UPDATE",
            pins_update,
        )
    return Response(status_code=204)


@router.post("/{channel_id}/ack", status_code=204)
async def acknowledge_channel(
    channel_id: EntityRef,
    payload: ReadStateUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("read_state.update"),
    )
    acknowledged = await channel_message(session, settings, channel, payload.message_id)
    statement = pg_insert(ReadState).values(
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
        user_is_local=True,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        last_message_id=acknowledged.id,
        last_message_domain=acknowledged.origin_domain,
        mention_count=0,
    )
    advances = ReadState.last_message_id.is_(None) | (
        tuple_(statement.excluded.last_message_id, statement.excluded.last_message_domain)
        >= tuple_(ReadState.last_message_id, ReadState.last_message_domain)
    )
    state = (
        await session.scalars(
            statement.on_conflict_do_update(
                index_elements=[
                    "user_id",
                    "user_domain",
                    "channel_id",
                    "channel_domain",
                ],
                set_={
                    "last_message_id": case(
                        (advances, statement.excluded.last_message_id),
                        else_=ReadState.last_message_id,
                    ),
                    "last_message_domain": case(
                        (advances, statement.excluded.last_message_domain),
                        else_=ReadState.last_message_domain,
                    ),
                    "mention_count": case((advances, 0), else_=ReadState.mention_count),
                    "updated_at": func.now(),
                },
            ).returning(ReadState)
        )
    ).one()
    await session.commit()
    unread = channel.last_message_id is not None and (
        state.last_message_id is None
        or (state.last_message_id, state.last_message_domain or "")
        < (channel.last_message_id, channel.last_message_domain or "")
    )
    await publish_dispatch(
        redis,
        user_topic(auth.user.origin_domain, auth.user.id),
        "READ_STATE_UPDATE",
        {
            "channel_id": str(channel.id),
            "channel_domain": channel.origin_domain,
            "last_message_id": str(state.last_message_id),
            "last_message_domain": state.last_message_domain,
            "mention_count": state.mention_count,
            "unread": unread,
        },
    )
    return Response(status_code=204)


async def require_typing_access(
    session: AsyncSession,
    redis: Redis,
    access: ChannelAccess,
    actor: User,
) -> None:
    channel = access.channel
    require_unarchived_thread(channel)
    await require_channel_permissions(
        session,
        redis,
        access,
        actor,
        required_permissions("typing.publish"),
    )
    if access.guild is not None:
        await require_member_interactions_allowed(
            session,
            access.guild,
            actor,
            Permission.SEND_MESSAGES,
        )


@router.post("/{channel_id}/typing", status_code=204)
async def typing(
    channel_id: EntityRef,
    response: Response,
    request: Request,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["typing"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    access = await load_channel_access(session, settings, auth.user, channel_id)
    channel = access.channel
    await require_typing_access(session, redis, access, auth.user)
    projection = new_typing_projection(channel, auth.user)
    if channel.origin_domain != settings.domain:
        publish_request = TypingPublishRequest(
            **projection.model_dump(mode="json"),
            actor=profile_from_user(auth.user),
        )
        try:
            upstream = await signed_request(
                session,
                settings,
                "POST",
                channel.origin_domain,
                "/_kaede/v1/typing/publish",
                payload=publish_request.model_dump(mode="json"),
                request_timeout=3,
                max_response_bytes=4096,
                guild_context=access.guild is not None,
            )
        except FederationNetworkError:
            raise HTTPException(
                status_code=503,
                detail={"code": "FEDERATED_TYPING_UNAVAILABLE"},
            ) from None
        if upstream.status_code != 204:
            detail: object = {"code": "FEDERATED_TYPING_REJECTED"}
            try:
                body = decode_federation_response_json(upstream, max_response_bytes=4096)
                if isinstance(body, dict) and isinstance(body.get("detail"), dict):
                    detail = body["detail"]
            except FederationNetworkError:
                pass
            raise HTTPException(status_code=upstream.status_code, detail=detail)
    else:
        sessionmaker = cast(async_sessionmaker[AsyncSession], request.app.state.sessionmaker)
        await publish_authoritative_typing(
            session,
            sessionmaker,
            redis,
            settings,
            channel,
            projection,
        )
    response.status_code = 204
    return response
