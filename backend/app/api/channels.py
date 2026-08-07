from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import cast

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from redis.asyncio import Redis
from sqlalchemy import case, delete, func, insert, select, tuple_, update
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
from app.chat.events import publish_dispatch, user_topic
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.payloads import attachment_payload, message_payload, render_message_payload
from app.chat.permissions import require_permissions
from app.chat.privacy import blocked_between, lock_relationship_pair, require_can_direct_message
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
    FederationEvent,
    FederationOutbox,
    GuildMember,
    Message,
    MessageProjection,
    Pin,
    Reaction,
    ReadState,
    User,
)
from app.federation.client import signed_request
from app.federation.events import build_envelope, queue_event
from app.federation.guilds import (
    GuildSequenceGap,
    apply_guild_message_event,
    assign_guild_sequence,
    remote_destinations_with_channel_access,
    store_guild_event,
    synchronize_guild,
)
from app.federation.network import FederationNetworkError
from app.federation.replication import profile_from_user
from app.federation.security import validated_event_envelope
from app.media.service import attachments_for_messages, finalize_attachment
from app.tasks import (
    SET_LATEST_MESSAGE_SCRIPT,
    federation_deliver,
    media_local_purge,
    media_process,
    mentions_fanout,
)

router = APIRouter(prefix="/api/v1/channels", tags=["messages"])
log = structlog.get_logger()


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
    actor: User,
    messages: list[Message],
) -> tuple[list[Attachment], set[str]]:
    refs = {(message.id, message.origin_domain) for message in messages}
    attachments = [
        item
        for rows in (await attachments_for_messages(session, refs)).values()
        for item in rows
        if item.origin_domain == settings.domain
    ]
    if not attachments:
        return [], set()
    if access.guild is None:
        destinations = {
            participant.origin_domain
            for participant in access.participants
            if participant.origin_domain != settings.domain
        }
    else:
        destinations = await remote_destinations_with_channel_access(
            session, settings, access.guild, access.channel
        )
    for attachment in attachments:
        envelope = await build_envelope(
            session,
            settings,
            "media.delete",
            actor,
            {
                "attachment_id": str(attachment.id),
                "origin_domain": attachment.origin_domain,
            },
        )
        for destination in destinations:
            await queue_event(session, settings, destination, envelope)
    return attachments, destinations


def require_local_mutation_authority(access: ChannelAccess, settings: Settings) -> None:
    remote_guild = access.guild is not None and access.guild.origin_domain != settings.domain
    remote_dm = access.guild is None and any(
        participant.origin_domain != settings.domain for participant in access.participants
    )
    if remote_guild or remote_dm:
        raise HTTPException(status_code=409, detail={"code": "FEDERATED_WRITE_UNSUPPORTED"})


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
) -> dict[tuple[int, str], str]:
    """Reconstruct sender-side federated DM delivery state from durable outbox rows."""
    local_ids = [
        str(message.id) for message in messages if message.author_domain == settings.domain
    ]
    if not local_ids:
        return {}
    rows = (
        await session.execute(
            select(FederationEvent.envelope, FederationOutbox.status)
            .join(
                FederationOutbox,
                (FederationOutbox.event_origin_domain == FederationEvent.origin_domain)
                & (FederationOutbox.event_id == FederationEvent.event_id),
            )
            .where(
                FederationEvent.origin_domain == settings.domain,
                FederationEvent.event_type == "dm.message.create",
                FederationEvent.envelope["content"]["message"]["channel_id"].astext
                == str(channel.id),
                FederationEvent.envelope["content"]["message"]["id"].astext.in_(local_ids),
            )
        )
    ).all()
    by_message: dict[tuple[int, str], list[str]] = {}
    for envelope, status_value in rows:
        message = envelope.get("content", {}).get("message", {})
        try:
            reference = (int(message["id"]), str(message["origin_domain"]))
        except (KeyError, TypeError, ValueError):
            continue
        by_message.setdefault(reference, []).append(str(status_value))
    result: dict[tuple[int, str], str] = {}
    for reference, statuses in by_message.items():
        if any(status in {"failed", "expired"} for status in statuses):
            result[reference] = "failed"
        elif all(status == "delivered" for status in statuses):
            result[reference] = "delivered"
        else:
            result[reference] = "pending"
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
    delivery_statuses = (
        await dm_delivery_statuses(session, settings, channel, messages)
        if access.guild is None
        else {}
    )
    payloads: list[dict[str, object]] = []
    for item in messages:
        payload = message_payload(
            item,
            authors.get((item.author_id, item.author_domain)),
            attachments.get((item.id, item.origin_domain), []),
        )
        delivery_status = delivery_statuses.get((item.id, item.origin_domain))
        if delivery_status is not None:
            payload["delivery_status"] = delivery_status
        payloads.append(payload)
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
    await require_dm_send(session, access, auth.user)
    if channel.type not in {0, 1, 5}:
        raise HTTPException(status_code=400, detail={"code": "NOT_TEXT_CHANNEL"})
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
    if payload.referenced_message_id is not None:
        referenced = await channel_message(
            session, settings, channel, payload.referenced_message_id
        )
    mention_pairs = list(
        dict.fromkeys(item.resolve(settings.domain) for item in payload.mention_user_ids)
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
            "mention_user_ids": [f"{user_id}@{domain}" for user_id, domain in mention_pairs],
            "attachments": [attachment_payload(item) for item in message_attachments],
        }
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
                        for user_id, domain in mention_pairs
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
        if response.status_code in {403, 404, 429}:
            try:
                error_body = response.json()
            except ValueError:
                error_body = None
            detail = parse_upstream_error(error_body, "FEDERATED_WRITE_REJECTED")
            raise HTTPException(status_code=response.status_code, detail=detail)
        if response.status_code != 200:
            raise HTTPException(status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"})
        try:
            proxied = response.json()
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
            if (
                event.get("type") != "guild.message.committed"
                or not isinstance(context, dict)
                or not isinstance(event_message, dict)
                or context.get("guild_id") != str(access.guild.id)
                or context.get("guild_domain") != access.guild.origin_domain
                or context.get("seq") != proxied.get("seq")
                or event_message != proxied["message"]
            ):
                raise ValueError("guild home returned a mismatched proxy event")
            try:
                replicated = await apply_guild_message_event(session, settings, access.guild, event)
            except GuildSequenceGap:
                await synchronize_guild(session, settings, access.guild)
                replicated = await apply_guild_message_event(session, settings, access.guild, event)
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"}
            ) from None
        except (httpx.HTTPError, FederationNetworkError, RuntimeError):
            raise HTTPException(
                status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"}
            ) from None
        await session.commit()
        if replicated is None:
            replicated = await session.get(
                Message,
                (int(event_message["id"]), event_message["origin_domain"]),
            )
        if replicated is None:
            raise RuntimeError("authoritative guild message was not replicated")
        for attachment in message_attachments:
            attachment.message_id = replicated.id
            attachment.message_domain = replicated.origin_domain
        await session.commit()
        for attachment in message_attachments:
            await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        result = message_payload(replicated, auth.user, message_attachments)
        await publish_channel_dispatch(redis, access, "MESSAGE_CREATE", result)
        return result
    message_id = await snowflake.mint()
    mention_refs = [
        {"id": str(user_id), "origin_domain": domain} for user_id, domain in mention_pairs
    ]
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
                client_nonce=payload.client_nonce,
                referenced_message_id=referenced.id if referenced is not None else None,
                referenced_message_domain=(
                    referenced.origin_domain if referenced is not None else None
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
        remote_destinations = {
            participant.origin_domain
            for participant in access.participants
            if participant.origin_domain != settings.domain
        }
        for destination in remote_destinations:
            envelope = await build_envelope(
                session,
                settings,
                "dm.message.create",
                auth.user,
                {
                    "message": message_payload(message, auth.user, message_attachments),
                    "author": profile_from_user(auth.user),
                },
            )
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
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    await require_channel_permissions(
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
    message.content = payload.content
    message.e2ee = payload.e2ee
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
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    await require_channel_permissions(
        session, redis, access, auth.user, required_permissions("message.delete.self")
    )
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
    access = await lock_local_channel_mutation(session, settings, access)
    if access.guild is None:
        raise HTTPException(status_code=400, detail={"code": "BULK_DELETE_NOT_SUPPORTED"})
    if access.guild.origin_domain != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "FEDERATED_WRITE_UNSUPPORTED"})
    await require_channel_permissions(
        session, redis, access, auth.user, required_permissions("message.bulk_delete")
    )
    message_refs = [item.resolve(settings.domain) for item in payload.message_ids]
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
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    await require_channel_permissions(
        session,
        redis,
        access,
        auth.user,
        required_permissions("reaction.create"),
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
            },
        )
    else:
        await session.commit()
    response.status_code = 204
    return response


@router.delete("/{channel_id}/messages/{message_id}/reactions/{emoji}", status_code=204)
async def remove_own_reaction(
    channel_id: EntityRef,
    message_id: EntityRef,
    response: Response,
    emoji: str = Path(min_length=1, max_length=255),
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
            },
        )
    response.status_code = 204
    return response


@router.delete("/{channel_id}/messages/{message_id}/reactions/{emoji}/{user_id}", status_code=204)
async def remove_user_reaction(
    channel_id: EntityRef,
    message_id: EntityRef,
    user_id: EntityRef,
    emoji: str = Path(min_length=1, max_length=255),
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
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    channel = access.channel
    await require_channel_permissions(
        session, redis, access, auth.user, required_permissions("pin.update")
    )
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
    require_local_mutation_authority(access, settings)
    access = await lock_local_channel_mutation(session, settings, access)
    await require_channel_permissions(
        session, redis, access, auth.user, required_permissions("pin.update")
    )
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
