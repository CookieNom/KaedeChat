from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import cast

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from redis.asyncio import Redis
from sqlalchemy import case, delete, exists, func, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.chat.channel_access import (
    ChannelAccess,
    load_channel_access,
    lock_local_channel_mutation,
    publish_channel_dispatch,
)
from app.chat.custom_emojis import validate_custom_emoji_use
from app.chat.e2ee import MessageEncryptionPolicyError, validate_message_encryption_policy
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.mentions import merge_mention_recipients, role_mention_recipients
from app.chat.payloads import (
    attachment_payload,
    dm_channel_payload,
    guild_payload,
    message_payload,
    render_message_payload,
    user_payload,
)
from app.chat.permissions import require_permissions
from app.chat.privacy import blocked_between, lock_relationship_pair, require_can_direct_message
from app.chat.reaction_payloads import reaction_payloads_for_messages
from app.chat.schemas import (
    MessageBulkDelete,
    MessageCreate,
    MessageEdit,
    ReactionCreate,
    ReadStateUpdate,
)
from app.core.errors import parse_upstream_error
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, EntityReferenceLike
from app.db.models import (
    Attachment,
    Channel,
    DMConversation,
    DMParticipant,
    E2EEDevice,
    FederationEvent,
    FederationOutbox,
    Guild,
    GuildMember,
    MediaTombstoneSource,
    Message,
    MessageProjection,
    Pin,
    Reaction,
    ReadState,
    TerminalRoomDeletion,
    User,
)
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
)
from app.federation.replica_storage import (
    REPLICA_QUOTA_ERROR_CODE,
    FederationReplicaQuotaExceeded,
    admit_replica_storage,
    mark_replica_quota_paused,
)
from app.federation.replication import profile_from_user
from app.federation.security import validated_event_envelope
from app.federation.terminal_rooms import lock_terminal_room
from app.media.service import attachments_for_messages, finalize_attachment
from app.media.tombstones import lock_media_tombstone_ref, queue_terminal_attachment_tombstone
from app.tasks import (
    SET_LATEST_MESSAGE_SCRIPT,
    federation_deliver,
    federation_guild_sync,
    media_local_purge,
    media_process,
    mentions_fanout,
)

router = APIRouter(prefix="/api/v1/channels", tags=["messages"])


async def require_owned_e2ee_sender_device(
    session: AsyncSession,
    user: User,
    envelope: object,
) -> None:
    if not isinstance(envelope, dict):
        raise HTTPException(status_code=409, detail={"code": "E2EE_SENDER_DEVICE_INVALID"})
    device_id = envelope.get("sender_device_id")
    device = await session.get(E2EEDevice, device_id) if isinstance(device_id, str) else None
    if (
        device is None
        or (device.user_id, device.user_domain) != (user.id, user.origin_domain)
        or device.revoked_at is not None
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_SENDER_DEVICE_INVALID"})


DM_REACTIONS_PER_MESSAGE_LIMIT = 100
log = structlog.get_logger()


def require_message_encryption_policy(
    channel: Channel,
    *,
    content: object,
    e2ee: object,
    attachment_count: int = 0,
) -> None:
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


async def require_channel_permissions(
    session: AsyncSession,
    redis: Redis,
    access: ChannelAccess,
    actor: User,
    permissions: Permission,
) -> int:
    if access.guild is not None:
        return await require_permissions(
            session,
            redis,
            access.guild,
            actor,
            permissions,
            channel=access.channel,
        )
    return int(Permission.EMBED_LINKS | Permission.ATTACH_FILES | Permission.SEND_MESSAGES)


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
    remote_dm = access.guild is None and any(
        participant.origin_domain != settings.domain for participant in access.participants
    )
    if remote_guild or remote_dm:
        raise HTTPException(status_code=409, detail={"code": "FEDERATED_WRITE_UNSUPPORTED"})


async def proxy_remote_guild_pin(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    message_ref: EntityReferenceLike,
    *,
    pinned: bool,
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
    if not isinstance(body, dict) or body.get("pinned") != pinned:
        raise HTTPException(status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"})
    return Response(status_code=204)


async def proxy_remote_guild_reaction(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    message_ref: EntityReferenceLike,
    emoji: str,
    *,
    remove: bool,
) -> Response:
    guild = access.guild
    if guild is None or guild.origin_domain == settings.domain:
        raise RuntimeError("reaction proxy requires a remote guild")
    message_id, message_domain = message_ref.resolve(settings.domain)
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/proxy-reaction",
            payload={
                "actor": profile_from_user(actor),
                "channel_id": str(access.channel.id),
                "message_id": f"{message_id}@{message_domain}",
                "emoji": emoji,
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
    if not isinstance(body, dict) or body.get("reacted") is not (not remove):
        raise HTTPException(status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"})
    return Response(status_code=204)


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
        )
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


@router.post("/{channel_id}/messages", status_code=status.HTTP_201_CREATED)
async def create_message(
    channel_id: EntityRef,
    payload: MessageCreate,
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
        CLIENT_RATE_LIMITS["message_send"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    access = await load_channel_access(session, settings, auth.user, channel_id)
    prelock_conversation = (
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
    elif prelock_conversation is not None and prelock_conversation.type == "group":
        await lock_terminal_room(
            session,
            "group_dm",
            prelock_conversation.id,
            prelock_conversation.origin_domain,
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
    elif prelock_conversation is not None and prelock_conversation.type == "group":
        terminal_receipt = await session.get(
            TerminalRoomDeletion,
            (
                "group_dm",
                prelock_conversation.id,
                prelock_conversation.origin_domain,
                settings.domain,
            ),
        )
        refreshed_conversation = await session.get(
            DMConversation,
            (prelock_conversation.id, prelock_conversation.origin_domain),
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
        prelock_conversation = refreshed_conversation
        access = ChannelAccess(
            channel=refreshed_channel,
            guild=None,
            participants=access.participants,
        )
    # Message publication and PhotoDNA terminalization share this canonical
    # fence. Acquire it before finalize_attachment takes FOR UPDATE so a
    # verdict can never deadlock the sender or commit between finalization and
    # recipient-route insertion.
    for attachment_id in sorted({int(item) for item in payload.attachment_ids}):
        await lock_media_tombstone_ref(session, attachment_id, settings.domain)
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    needed = required_permissions("message.create")
    if payload.attachment_ids:
        needed |= Permission.ATTACH_FILES
    actor_permissions = await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        needed,
    )
    await validate_custom_emoji_use(
        session,
        auth.user,
        payload.content,
        target_guild=access.guild,
        target_permissions=actor_permissions,
    )
    await require_dm_send(session, access, auth.user)
    if channel.type not in {0, 1, 5}:
        raise HTTPException(status_code=400, detail={"code": "NOT_TEXT_CHANNEL"})
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
    explicit_mention_pairs = list(
        dict.fromkeys(item.resolve(settings.domain) for item in payload.mention_user_ids)
    )
    mention_pairs = explicit_mention_pairs
    if access.guild is not None:
        mention_pairs = merge_mention_recipients(
            mention_pairs,
            await role_mention_recipients(
                session, access.guild, payload.content, actor_permissions
            ),
        )
    allowed_mentions = {
        (participant.id, participant.origin_domain) for participant in access.participants
    }
    if access.guild is not None and mention_pairs:
        mention_rows = (
            await session.execute(
                select(GuildMember.user_id, GuildMember.user_domain).where(
                    GuildMember.guild_id == access.guild.id,
                    GuildMember.guild_domain == access.guild.origin_domain,
                    tuple_(GuildMember.user_id, GuildMember.user_domain).in_(mention_pairs),
                )
            )
        ).all()
        allowed_mentions = {(user_id, domain) for user_id, domain in mention_rows}
    if any(item not in allowed_mentions for item in mention_pairs):
        raise HTTPException(status_code=400, detail={"code": "INVALID_MENTION"})
    message_attachments: list[Attachment] = []
    for attachment_id in payload.attachment_ids:
        attachment = await finalize_attachment(
            session,
            settings,
            auth.user,
            int(attachment_id),
            required_purpose="attachment",
        )
        if attachment.message_id is not None or attachment.message_domain is not None:
            raise HTTPException(status_code=409, detail={"code": "ATTACHMENT_ALREADY_USED"})
        message_attachments.append(attachment)
    require_message_encryption_policy(
        channel,
        content=payload.content,
        e2ee=payload.e2ee,
        attachment_count=len(message_attachments),
    )
    if channel.encryption_mode == "e2ee":
        await require_owned_e2ee_sender_device(session, auth.user, payload.e2ee)
    if channel.encryption_mode == "e2ee" and (
        not isinstance(payload.e2ee, dict)
        or payload.e2ee.get("operation") != "create"
        or "target_message" in payload.e2ee
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_INVALID"})
    expected_attachment_mode = "e2ee" if channel.encryption_mode == "e2ee" else "plaintext"
    if any(item.encryption_mode != expected_attachment_mode for item in message_attachments):
        raise HTTPException(
            status_code=409,
            detail={"code": "MESSAGE_ENCRYPTION_POLICY_INVALID"},
        )
    if access.guild is not None and channel.rate_limit_per_user:
        slowmode_key = (
            f"slowmode:{channel.origin_domain}:{channel.id}:"
            f"{auth.user.origin_domain}:{auth.user.id}"
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
                    "retry_after_ms": await slowmode_retry_after_ms(redis, slowmode_key),
                },
            )
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        if payload.client_nonce is None:
            raise HTTPException(
                status_code=400, detail={"code": "CLIENT_NONCE_REQUIRED_FOR_FEDERATION"}
            )
        proxy_payload = {
            "operation": "message.create",
            "actor": profile_from_user(auth.user),
            "channel_id": str(channel.id),
            "content": payload.content,
            "e2ee": payload.e2ee,
            "client_nonce": payload.client_nonce,
            "referenced_message_id": (
                f"{referenced.id}@{referenced.origin_domain}" if referenced is not None else None
            ),
            "mention_user_ids": [
                f"{user_id}@{domain}" for user_id, domain in explicit_mention_pairs
            ],
            "attachments": [attachment_payload(item) for item in message_attachments],
        }
        if message_attachments:
            # The remote guild home can commit this proposal and fan its
            # attachment metadata out before our HTTP response is observed.
            # Durably remember that authority before making the request so a
            # crash cannot strand a later terminal media tombstone.
            await record_attachment_recipients(
                session,
                {(item.id, item.origin_domain) for item in message_attachments},
                access.guild.origin_domain,
                room_ref=("guild", access.guild.id, access.guild.origin_domain),
            )
            await session.commit()
        replica_was_quota_paused = access.guild.sync_status == "quota_paused"
        try:
            response = await signed_request(
                session,
                settings,
                "POST",
                access.guild.origin_domain,
                f"/_kaede/v1/guilds/{access.guild.id}/proxy",
                payload=proxy_payload,
            )
        except (httpx.HTTPError, FederationNetworkError, RuntimeError):
            envelope = await build_envelope(
                session,
                settings,
                "guild.proxy.message.create",
                auth.user,
                {
                    "actor": profile_from_user(auth.user),
                    "channel_id": str(channel.id),
                    "content": payload.content,
                    "e2ee": payload.e2ee,
                    "client_nonce": payload.client_nonce,
                    "referenced_message_ref": (
                        {
                            "id": str(referenced.id),
                            "origin_domain": referenced.origin_domain,
                        }
                        if referenced is not None
                        else None
                    ),
                    "mention_user_refs": [
                        {"id": str(user_id), "origin_domain": domain}
                        for user_id, domain in explicit_mention_pairs
                    ],
                    "attachments": [attachment_payload(item) for item in message_attachments],
                },
                context={
                    "guild_id": str(access.guild.id),
                    "guild_domain": access.guild.origin_domain,
                },
            )
            await queue_event(session, settings, access.guild.origin_domain, envelope)
            await session.commit()
            await enqueue_best_effort(federation_deliver, access.guild.origin_domain)
            for attachment in message_attachments:
                await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
            response_status.status_code = status.HTTP_202_ACCEPTED
            return {"status": "queued", "client_nonce": payload.client_nonce}
        raise_proxy_rejection(response, {403, 404, 429, 507})
        if response.status_code != 200:
            raise HTTPException(status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"})
        try:
            proxied = decode_federation_response_json(response)
        except FederationNetworkError:
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"}
            ) from None
        try:
            if (
                not isinstance(proxied, dict)
                or not isinstance(proxied.get("message"), dict)
                or not isinstance(proxied.get("event"), dict)
            ):
                raise ValueError("guild home returned an invalid proxy response")
            committed_envelope = await validated_event_envelope(
                session,
                settings,
                access.guild.origin_domain,
                proxied["event"],
            )
            event = committed_envelope.model_dump(mode="json")
            context = event.get("context")
            content = event.get("content")
            event_message = content.get("message") if isinstance(content, dict) else None
            expected_attachment_payloads = [
                attachment_payload(item) for item in message_attachments
            ]
            expected_remote_attachment_payloads = []
            for expected_attachment in expected_attachment_payloads:
                projected_attachment = dict(expected_attachment)
                projected_attachment["scan_status"] = (
                    "encrypted"
                    if projected_attachment.get("encryption_mode") == "e2ee"
                    else "clean"
                )
                expected_remote_attachment_payloads.append(projected_attachment)
            expected_attachment_refs = {
                (item.id, item.origin_domain) for item in message_attachments
            }
            if (
                event.get("type") != "guild.message.committed"
                or not isinstance(context, dict)
                or not isinstance(event_message, dict)
                or context.get("guild_id") != str(access.guild.id)
                or context.get("guild_domain") != access.guild.origin_domain
                or context.get("seq") != proxied.get("seq")
                or event_message != proxied["message"]
                or event_message.get("origin_domain") != access.guild.origin_domain
                or event_message.get("channel_id") != str(channel.id)
                or event_message.get("channel_domain") != channel.origin_domain
                or event_message.get("author_id") != str(auth.user.id)
                or event_message.get("author_domain") != auth.user.origin_domain
                or event_message.get("content") != payload.content
                or event_message.get("e2ee") != payload.e2ee
                or event_message.get("client_nonce") != payload.client_nonce
                # The authority is allowed to project the lifecycle status it
                # assigns while validating the proxy request (plaintext
                # references become ``clean`` and E2EE references become
                # ``encrypted``).  Every immutable metadata field and the
                # exact ordered reference set must still match the request.
                or event_message.get("attachments") != expected_remote_attachment_payloads
                or message_attachment_refs(event) != expected_attachment_refs
                or event_message.get("referenced_message_id")
                != (str(referenced.id) if referenced is not None else None)
                or event_message.get("referenced_message_domain")
                != (referenced.origin_domain if referenced is not None else None)
            ):
                raise ValueError("guild home returned a mismatched proxy event")
            # The request crossed a transaction boundary while the remote home
            # committed the proposal. Re-enter the room/media fence and reload
            # the projection before applying or binding anything: an exact
            # terminal guild control may have won while HTTP was in flight.
            await lock_terminal_room(
                session,
                "guild",
                access.guild.id,
                access.guild.origin_domain,
            )
            for attachment_id, attachment_domain in sorted(
                expected_attachment_refs, key=lambda item: (item[1], item[0])
            ):
                await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
            terminal_receipt = await session.get(
                TerminalRoomDeletion,
                (
                    "guild",
                    access.guild.id,
                    access.guild.origin_domain,
                    settings.domain,
                ),
            )
            live_guild = await session.get(
                Guild,
                (access.guild.id, access.guild.origin_domain),
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
                    await enqueue_best_effort(
                        media_process, attachment.id, attachment.origin_domain
                    )
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
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"}
            ) from None
        except (httpx.HTTPError, FederationNetworkError, RuntimeError):
            raise HTTPException(
                status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"}
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
        result = message_payload(replicated, auth.user, message_attachments)
        await publish_channel_dispatch(redis, access, "MESSAGE_CREATE", result)
        return result
    message_id = await snowflake.mint()
    if referenced_ref is not None and referenced_ref >= (message_id, settings.domain):
        raise HTTPException(status_code=400, detail={"code": "INVALID_MESSAGE_REFERENCE"})
    mention_refs = [
        {"id": str(user_id), "origin_domain": domain} for user_id, domain in mention_pairs
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
                    attachments=message_attachments,
                    client_nonce=payload.client_nonce,
                ),
                protected_refs=(
                    {referenced_ref}
                    if referenced is not None and referenced_ref is not None
                    else None
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
                encryption_policy_generation=channel.encryption_policy_generation,
                encryption_epoch=channel.encryption_epoch,
                client_nonce=payload.client_nonce,
                referenced_message_id=(referenced_ref[0] if referenced_ref is not None else None),
                referenced_message_domain=(
                    referenced_ref[1] if referenced_ref is not None else None
                ),
                mention_user_refs=mention_refs,
                flags=(0 if actor_permissions & Permission.EMBED_LINKS else 4),
            )
            .returning(Message)
        )
    ).one()
    for attachment in message_attachments:
        attachment.message_id = message.id
        attachment.message_domain = message.origin_domain
    remote_destinations: set[str] = set()
    if access.guild is None:
        conversation = dm_conversation
        if conversation is None:
            raise RuntimeError("direct-message channel has no conversation")
        message_content = {
            "message": message_payload(message, auth.user, message_attachments),
            "author": profile_from_user(auth.user),
        }
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
            )
        for destination in remote_destinations:
            await queue_event(session, settings, destination, envelope)
    elif access.guild.origin_domain == settings.domain:
        remote_destinations = await remote_destinations_with_channel_access(
            session, settings, access.guild, channel
        )
        if remote_destinations:
            seq = await assign_guild_sequence(session, access.guild)
            envelope = await build_envelope(
                session,
                settings,
                "guild.message.create",
                auth.user,
                {
                    "message": message_payload(message, auth.user, message_attachments),
                    "author": profile_from_user(auth.user),
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
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            mention_user_refs=mention_refs,
        )
    )
    await session.commit()
    result = message_payload(message, auth.user, message_attachments)
    if access.guild is None:
        result["delivery_status"] = "pending" if remote_destinations else "delivered"
    try:
        await cast(
            Awaitable[object],
            redis.eval(
                SET_LATEST_MESSAGE_SCRIPT,
                1,
                f"channel:last_message:{channel.origin_domain}:{channel.id}",
                str(message.id),
                message.origin_domain,
            ),
        )
        await publish_channel_dispatch(redis, access, "MESSAGE_CREATE", result)
        if access.guild is None and dm_history_changed and dm_conversation is not None:
            history = dm_history_metadata(
                dm_conversation,
                local_domain=settings.domain,
                remote_available=await dm_authority_history_available(
                    session,
                    dm_conversation,
                    local_domain=settings.domain,
                ),
            )
            for participant in access.participants:
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
                            for user in access.participants
                            if (user.id, user.origin_domain)
                            != (participant.id, participant.origin_domain)
                        ],
                        conversation=dm_conversation,
                        history=history,
                    ),
                )
    except Exception:
        log.exception(
            "message_postcommit_projection_failed",
            message_id=str(message.id),
            message_domain=message.origin_domain,
        )
    # These workflows have durable SQL sources and must still be woken when an
    # unrelated Redis cache/fanout projection above fails.
    await enqueue_best_effort(mentions_fanout, message.id, message.origin_domain)
    for destination in remote_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    for attachment in message_attachments:
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
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
) -> dict[str, object]:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    require_local_mutation_authority(access, settings)
    if access.guild is not None:
        await lock_terminal_room(
            session,
            "guild",
            access.guild.id,
            access.guild.origin_domain,
        )
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    actor_permissions = await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("message.edit.self"),
    )
    await require_dm_send(session, access, auth.user)
    message = await channel_message(
        session,
        settings,
        channel,
        message_id,
        for_update=True,
        require_active=True,
    )
    if (message.author_id, message.author_domain) != (auth.user.id, auth.user.origin_domain):
        # Moderation is intentionally delete-only. Editing another user's
        # content while preserving their authorship would be impersonation.
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    require_message_encryption_policy(
        channel,
        content=payload.content,
        e2ee=payload.e2ee,
    )
    if channel.encryption_mode == "e2ee":
        await require_owned_e2ee_sender_device(session, auth.user, payload.e2ee)
    if channel.encryption_mode == "e2ee" and (
        not isinstance(payload.e2ee, dict)
        or payload.e2ee.get("operation") != "edit"
        or payload.e2ee.get("target_message") != f"{message.id}@{message.origin_domain}"
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_INVALID"})
    await validate_custom_emoji_use(
        session,
        auth.user,
        payload.content,
        target_guild=access.guild,
        target_permissions=actor_permissions,
    )
    message.content = payload.content
    message.e2ee = payload.e2ee
    message.encryption_policy_generation = channel.encryption_policy_generation
    message.encryption_epoch = channel.encryption_epoch
    message.edited_at = datetime.now(UTC)
    # An update payload is a complete replacement in both gateway clients and
    # remote projections. Preserve the stored attachment set on content-only
    # edits instead of accidentally serializing it as an empty list.
    result = await render_message_payload(session, message, auth.user)
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
    await session.commit()
    if access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    await publish_channel_dispatch(redis, access, "MESSAGE_UPDATE", result)
    return result


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
    require_local_mutation_authority(access, settings)
    if access.guild is not None:
        await lock_terminal_room(
            session,
            "guild",
            access.guild.id,
            access.guild.origin_domain,
        )
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    await require_channel_permissions(
        session, redis, access, auth.user, required_permissions("message.delete.self")
    )
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
    message = await channel_message(session, settings, channel, message_id, for_update=True)
    if (message.author_id, message.author_domain) != (auth.user.id, auth.user.origin_domain):
        if access.guild is None:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("message.delete.other")
        )
    already_deleted = message.deleted_at is not None
    if not already_deleted:
        message.content = None
        message.e2ee = None
        message.deleted_at = datetime.now(UTC)
        deleted_attachments, media_destinations = await queue_attachment_tombstones(
            session, settings, access, auth.user, [message]
        )
        if access.guild is not None:
            await queue_guild_mutation(
                session,
                settings,
                access.guild,
                auth.user,
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
    else:
        deleted_attachments, media_destinations = [], set()
    await session.commit()
    if not already_deleted and access.guild is not None:
        await wake_queued_guild_federation(access.guild)
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
    require_local_mutation_authority(access, settings)
    if access.guild is not None:
        await lock_terminal_room(
            session,
            "guild",
            access.guild.id,
            access.guild.origin_domain,
        )
    access = await lock_local_channel_mutation(session, settings, access)
    if access.guild is None:
        raise HTTPException(status_code=400, detail={"code": "BULK_DELETE_NOT_SUPPORTED"})
    if access.guild.origin_domain != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "FEDERATED_WRITE_UNSUPPORTED"})
    await require_channel_permissions(
        session, redis, access, auth.user, required_permissions("message.bulk_delete")
    )
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
    deleted_at = datetime.now(UTC)
    for deleted_message in messages:
        deleted_message.content = None
        deleted_message.e2ee = None
        deleted_message.deleted_at = deleted_at
    deleted_attachments, media_destinations = await queue_attachment_tombstones(
        session, settings, access, auth.user, messages
    )
    await session.execute(
        update(Message)
        .where(
            tuple_(Message.id, Message.origin_domain).in_(message_refs),
            Message.channel_id == access.channel.id,
            Message.channel_domain == access.channel.origin_domain,
        )
        .values(content=None, e2ee=None, deleted_at=deleted_at)
    )
    for deleted_message in messages:
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            auth.user,
            "guild.message.delete",
            {
                "message": {
                    "id": str(deleted_message.id),
                    "origin_domain": deleted_message.origin_domain,
                },
                "deleted_at": deleted_at.isoformat(),
            },
            channel=access.channel,
        )
    await session.commit()
    await wake_queued_guild_federation(access.guild)
    for attachment in deleted_attachments:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
    for destination in media_destinations:
        await enqueue_best_effort(federation_deliver, destination)
    for deleted_message in messages:
        await publish_channel_dispatch(
            redis,
            access,
            "MESSAGE_DELETE",
            {
                "id": str(deleted_message.id),
                "origin_domain": deleted_message.origin_domain,
                "channel_id": str(access.channel.id),
                "channel_domain": access.channel.origin_domain,
            },
        )
    return Response(status_code=204)


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
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("reaction.create")
        )
        return await proxy_remote_guild_reaction(
            session,
            settings,
            access,
            auth.user,
            message_id,
            payload.emoji,
            remove=False,
        )
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
    await require_dm_send(session, access, auth.user)
    await validate_custom_emoji_use(
        session,
        auth.user,
        payload.emoji,
        target_guild=access.guild,
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
    if inserted is not None:
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
        await session.commit()
        if access.guild is not None:
            await wake_queued_guild_federation(access.guild)
        await publish_channel_dispatch(
            redis,
            access,
            "MESSAGE_UPDATE",
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(access.channel.id),
                "channel_domain": access.channel.origin_domain,
                "reaction": payload.emoji,
                "user_id": str(auth.user.id),
                "user_domain": auth.user.origin_domain,
            },
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

    access = await load_channel_access(session, settings, auth.user, channel_id)
    await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("reaction.list"),
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


@router.delete("/{channel_id}/messages/{message_id}/reactions/{emoji}", status_code=204)
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
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["reaction"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    access = await load_channel_access(session, settings, auth.user, channel_id)
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
    if removed is not None and access.guild is not None:
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
    await session.commit()
    if removed is not None and access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    if removed is not None:
        await publish_channel_dispatch(
            redis,
            access,
            "MESSAGE_UPDATE",
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(access.channel.id),
                "channel_domain": access.channel.origin_domain,
                "reaction": emoji,
                "removed": True,
                "user_id": str(auth.user.id),
                "user_domain": auth.user.origin_domain,
            },
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
    access = await load_channel_access(session, settings, auth.user, channel_id)
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    user_number, user_domain = user_id.resolve(settings.domain)
    if (user_number, user_domain) != (auth.user.id, auth.user.origin_domain):
        if access.guild is None:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
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
            "MESSAGE_UPDATE",
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(access.channel.id),
                "channel_domain": access.channel.origin_domain,
                "reaction": emoji,
                "removed": True,
                "user_id": str(user_number),
                "user_domain": user_domain,
            },
        )
    return Response(status_code=204)


@router.put("/{channel_id}/pins/{message_id}", status_code=204)
async def pin_message(
    channel_id: EntityRef,
    message_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("pin.update")
        )
        return await proxy_remote_guild_pin(
            session, settings, access, auth.user, message_id, pinned=True
        )
    # Guild pins are shared federation state and are committed by the guild
    # home. DM pins are intentionally a home-local saved view: a direct
    # conversation has no safe total ordering for concurrent pin mutations
    # from two independent homes.
    if access.guild is not None:
        require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
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
    await session.commit()
    if inserted is not None and access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    if inserted is not None:
        await publish_channel_dispatch(
            redis,
            access,
            "MESSAGE_UPDATE",
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "pinned": True,
            },
        )
    return Response(status_code=204)


@router.get("/{channel_id}/pins")
async def list_pins(
    channel_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    if access.guild is not None:
        await require_channel_permissions(
            session,
            redis,
            access,
            auth.user,
            required_permissions("pin.list"),
        )
    rows = (
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
            .where(
                Pin.channel_id == access.channel.id,
                Pin.channel_domain == access.channel.origin_domain,
            )
            .order_by(Pin.pinned_at.desc(), Pin.message_id.desc())
        )
    ).all()
    reaction_payloads = await reaction_payloads_for_messages(
        session,
        {(message.id, message.origin_domain) for _, message, _ in rows},
        viewer=auth.user,
    )
    attachments = await attachments_for_messages(
        session, {(message.id, message.origin_domain) for _, message, _ in rows}
    )
    return [
        {
            **message_payload(
                message,
                author,
                attachments.get((message.id, message.origin_domain), []),
            ),
            "reaction_counts": reaction_payloads.get((message.id, message.origin_domain), ({}, []))[
                0
            ],
            "reacted_emoji": reaction_payloads.get((message.id, message.origin_domain), ({}, []))[
                1
            ],
            "pinned_at": pin.pinned_at.isoformat(),
        }
        for pin, message, author in rows
    ]


@router.delete("/{channel_id}/pins/{message_id}", status_code=204)
async def unpin_message(
    channel_id: EntityRef,
    message_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    if access.guild is not None and access.guild.origin_domain != settings.domain:
        await require_channel_permissions(
            session, redis, access, auth.user, required_permissions("pin.update")
        )
        return await proxy_remote_guild_pin(
            session, settings, access, auth.user, message_id, pinned=False
        )
    if access.guild is not None:
        require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
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
    await session.commit()
    if removed is not None and access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    if removed is not None:
        await publish_channel_dispatch(
            redis,
            access,
            "MESSAGE_UPDATE",
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(access.channel.id),
                "channel_domain": access.channel.origin_domain,
                "pinned": False,
            },
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


@router.post("/{channel_id}/typing", status_code=204)
async def typing(
    channel_id: EntityRef,
    response: Response,
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
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("typing.publish"),
    )
    await require_dm_send(session, access, auth.user)
    await publish_channel_dispatch(
        redis,
        access,
        "TYPING_START",
        {
            "channel_id": str(channel.id),
            "channel_domain": channel.origin_domain,
            "user_id": str(auth.user.id),
            "user_domain": auth.user.origin_domain,
            "timestamp": int(datetime.now(UTC).timestamp()),
        },
    )
    response.status_code = 204
    return response
