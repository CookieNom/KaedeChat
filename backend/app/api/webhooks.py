from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import cast

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import Field, field_validator, model_validator
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
from app.api.guilds import guild_channel, local_guild
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.payloads import message_payload
from app.chat.permissions import require_permissions
from app.chat.schemas import RequestModel
from app.core.permission_contract import required_permissions
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, EntityReference, Snowflake
from app.db.models import Message, MessageProjection, User, Webhook
from app.federation.guilds import remote_destinations_with_channel_access
from app.federation.replication import profile_from_user
from app.tasks import SET_LATEST_MESSAGE_SCRIPT, federation_deliver, mentions_fanout

router = APIRouter(tags=["webhooks"])
log = structlog.get_logger()
WEBHOOK_RATE_SCRIPT = """
local attempts = redis.call('INCR', KEYS[1])
if attempts == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return attempts
"""


class WebhookCreate(RequestModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned


class WebhookPatch(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    avatar_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def has_change(self) -> WebhookPatch:
        if not self.model_fields_set:
            raise ValueError("at least one webhook field is required")
        return self


class WebhookExecute(RequestModel):
    content: str = Field(min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def meaningful_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


def new_webhook_token() -> str:
    return f"kwh_{secrets.token_urlsafe(32)}"


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def webhook_payload(webhook: Webhook, *, token: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "id": str(webhook.id),
        "guild_id": str(webhook.guild_id),
        "guild_domain": webhook.guild_domain,
        "channel_id": str(webhook.channel_id),
        "channel_domain": webhook.channel_domain,
        "name": webhook.name,
        "avatar_hash": webhook.avatar_hash,
        "revoked": webhook.revoked_at is not None,
    }
    if token is not None:
        result["token"] = token
    return result


@router.post("/api/v1/guilds/{guild_id}/channels/{channel_id}/webhooks", status_code=201)
async def create_webhook(
    guild_id: EntityRef,
    channel_id: EntityRef,
    payload: WebhookCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    guild = await local_guild(session, settings, guild_id)
    channel = await guild_channel(session, settings, guild_id, channel_id)
    if channel.type not in {0, 5}:
        raise HTTPException(status_code=400, detail={"code": "WEBHOOK_REQUIRES_TEXT_CHANNEL"})
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("webhook.manage"),
        channel=channel,
    )
    token = new_webhook_token()
    webhook = Webhook(
        id=await snowflake.mint(),
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        name=payload.name,
        token_hash=token_digest(token),
        creator_id=auth.user.id,
        creator_domain=auth.user.origin_domain,
    )
    session.add(webhook)
    await session.commit()
    return webhook_payload(webhook, token=token)


async def manageable_webhook(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    webhook_id: int,
) -> Webhook:
    webhook = await session.scalar(
        select(Webhook).where(Webhook.id == webhook_id).with_for_update()
    )
    if webhook is None or webhook.revoked_at is not None:
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_NOT_FOUND"})
    guild = await local_guild(session, settings, EntityReference(webhook.guild_id))
    channel = await guild_channel(
        session,
        settings,
        EntityReference(webhook.guild_id),
        EntityReference(webhook.channel_id),
    )
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("webhook.manage"),
        channel=channel,
    )
    return webhook


@router.get("/api/v1/guilds/{guild_id}/webhooks")
async def list_webhooks(
    guild_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    guild = await local_guild(session, settings, guild_id)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.webhook.list")
    )
    rows = list(
        await session.scalars(
            select(Webhook)
            .where(
                Webhook.guild_id == guild.id,
                Webhook.guild_domain == guild.origin_domain,
                Webhook.revoked_at.is_(None),
            )
            .order_by(Webhook.id)
        )
    )
    return [webhook_payload(item) for item in rows]


@router.patch("/api/v1/webhooks/{webhook_id}")
async def patch_webhook(
    webhook_id: Snowflake,
    payload: WebhookPatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    webhook = await manageable_webhook(session, redis, settings, auth, int(webhook_id))
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(webhook, field, value)
    await session.commit()
    return webhook_payload(webhook)


@router.post("/api/v1/webhooks/{webhook_id}/rotate")
async def rotate_webhook(
    webhook_id: Snowflake,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    webhook = await manageable_webhook(session, redis, settings, auth, int(webhook_id))
    token = new_webhook_token()
    webhook.token_hash = token_digest(token)
    await session.commit()
    return webhook_payload(webhook, token=token)


@router.delete("/api/v1/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: Snowflake,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    webhook = await manageable_webhook(session, redis, settings, auth, int(webhook_id))
    webhook.revoked_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=204)


def request_webhook_token(path_token: str | None, authorization: str | None) -> str:
    if path_token is not None:
        return path_token
    if authorization is not None and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ")
    return ""


@router.post("/api/v1/webhooks/{webhook_id}/{path_token}", status_code=201)
@router.post("/api/v1/webhooks/{webhook_id}", status_code=201)
async def execute_webhook(
    webhook_id: Snowflake,
    payload: WebhookExecute,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=128
    ),
    path_token: str | None = None,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    del request
    webhook = await session.scalar(
        select(Webhook).where(Webhook.id == int(webhook_id)).with_for_update()
    )
    supplied = request_webhook_token(path_token, authorization)
    if (
        webhook is None
        or webhook.revoked_at is not None
        or not supplied.startswith("kwh_")
        or not hmac.compare_digest(token_digest(supplied), webhook.token_hash)
    ):
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_NOT_FOUND"})
    rate_key = f"rate:webhook:{webhook.id}"
    attempts = int(
        cast(
            int | str,
            await cast(
                Awaitable[object],
                redis.eval(WEBHOOK_RATE_SCRIPT, 1, rate_key, "2"),
            ),
        )
    )
    if attempts > 5:
        raise HTTPException(
            status_code=429,
            detail={"code": "WEBHOOK_RATE_LIMITED", "retry_after_ms": 2000},
        )
    guild = await local_guild(session, settings, EntityReference(webhook.guild_id), for_update=True)
    channel = await guild_channel(
        session,
        settings,
        EntityReference(webhook.guild_id),
        EntityReference(webhook.channel_id),
    )
    if channel.type not in {0, 5}:
        raise HTTPException(status_code=400, detail={"code": "WEBHOOK_REQUIRES_TEXT_CHANNEL"})
    creator = await session.get(User, (webhook.creator_id, webhook.creator_domain))
    if creator is None:
        raise HTTPException(status_code=410, detail={"code": "WEBHOOK_CREATOR_MISSING"})
    nonce = (
        f"w{hashlib.blake2s(idempotency_key.encode(), digest_size=16).hexdigest()}"
        if idempotency_key is not None
        else None
    )
    if nonce is not None:
        existing = await session.scalar(
            select(Message).where(
                Message.webhook_id == webhook.id,
                Message.channel_id == channel.id,
                Message.channel_domain == channel.origin_domain,
                Message.client_nonce == nonce,
            )
        )
        if existing is not None:
            return message_payload(existing, None)
    message = Message(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=creator.id,
        author_domain=creator.origin_domain,
        content=payload.content,
        message_type=2,
        webhook_id=webhook.id,
        webhook_name=webhook.name,
        webhook_avatar_hash=webhook.avatar_hash,
        client_nonce=nonce,
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
    rendered = message_payload(message, None)
    destinations = await remote_destinations_with_channel_access(session, settings, guild, channel)
    if destinations:
        await queue_guild_mutation(
            session,
            settings,
            guild,
            creator,
            "guild.message.create",
            {"message": rendered, "author": profile_from_user(creator)},
            channel=channel,
        )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "MESSAGE_CREATE", rendered
    )
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
    except Exception:
        # The SQL projection row is durable and the scheduled sweep repairs the
        # cache. Never report a failed webhook after its message committed.
        log.exception("webhook_latest_message_wake_failed", webhook_id=webhook.id)
    await enqueue_best_effort(mentions_fanout, message.id, message.origin_domain)
    for destination in destinations:
        await enqueue_best_effort(federation_deliver, destination)
    return rendered
