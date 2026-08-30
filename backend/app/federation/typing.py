from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, cast

import structlog
from fastapi import HTTPException
from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.events import publish_ephemeral, user_topic
from app.chat.permissions import calculate_permissions
from app.core.model_validation import UnambiguousInputModel
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import Channel, DMConversation, DMParticipant, Guild, GuildMember, User
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError
from app.federation.schemas import FederationDomain, RemoteUserProfile, SnowflakeString

log = structlog.get_logger()

TYPING_TTL_SECONDS = 10
TYPING_FUTURE_SKEW_SECONDS = 5
TYPING_RELAY_BATCH_SIZE = 8
TYPING_AUDIENCE_BATCH_SIZE = 512
TYPING_MAX_AUDIENCE_BATCHES = 512

ACCEPT_TYPING_GENERATION_SCRIPT = """
local incoming = tonumber(ARGV[1])
local latest = tonumber(redis.call('GET', KEYS[1]) or 0)
if incoming < latest then return 0 end
if incoming > latest then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
end
local batch_latest = tonumber(redis.call('GET', KEYS[2]) or 0)
if incoming <= batch_latest then return 0 end
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
return 1
"""


class TypingProjection(UnambiguousInputModel):
    """Short-lived, authority-routable typing metadata with no message body."""

    model_config = ConfigDict(extra="forbid")

    channel_id: SnowflakeString
    channel_domain: FederationDomain
    user_id: SnowflakeString
    user_domain: FederationDomain
    observed_at: StrictInt = Field(ge=1)
    expires_at: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def validate_canonical_window(self) -> TypingProjection:
        if int(self.channel_id) <= 0 or int(self.user_id) <= 0:
            raise ValueError("typing references must be positive snowflakes")
        observed_seconds = self.observed_at // 1_000_000
        if not observed_seconds < self.expires_at <= observed_seconds + TYPING_TTL_SECONDS + 1:
            raise ValueError("typing expiry is inconsistent")
        return self


class TypingPublishRequest(TypingProjection):
    """User-home request sent to the exact channel authority."""

    actor: RemoteUserProfile

    @model_validator(mode="after")
    def validate_actor_binding(self) -> TypingPublishRequest:
        if (self.actor.id, self.actor.origin_domain) != (self.user_id, self.user_domain):
            raise ValueError("typing actor profile does not match its routing reference")
        return self


class TypingRelayRequest(TypingProjection):
    """Room-authority relay bound to an exact destination-home audience."""

    audience_user_refs: list[EntityRef] = Field(
        min_length=1,
        max_length=TYPING_AUDIENCE_BATCH_SIZE,
    )
    batch_index: StrictInt = Field(ge=0, lt=TYPING_MAX_AUDIENCE_BATCHES)
    batch_count: StrictInt = Field(ge=1, le=TYPING_MAX_AUDIENCE_BATCHES)

    @field_validator("audience_user_refs")
    @classmethod
    def canonical_audience(cls, value: list[EntityRef]) -> list[EntityRef]:
        if any(item.domain is None or item.id <= 0 for item in value):
            raise ValueError("typing relay audience references must be fully qualified")
        if value != sorted(set(value), key=str):
            raise ValueError("typing relay audience must be sorted and unique")
        return value

    @model_validator(mode="after")
    def valid_batch(self) -> TypingRelayRequest:
        if self.batch_index >= self.batch_count:
            raise ValueError("typing relay batch index is invalid")
        return self


def new_typing_projection(channel: Channel, actor: User) -> TypingProjection:
    observed_at = time.time_ns() // 1_000
    return TypingProjection(
        channel_id=str(channel.id),
        channel_domain=channel.origin_domain,
        user_id=str(actor.id),
        user_domain=actor.origin_domain,
        observed_at=observed_at,
        expires_at=observed_at // 1_000_000 + TYPING_TTL_SECONDS,
    )


def typing_projection_is_fresh(
    projection: TypingProjection,
    *,
    now: int | None = None,
) -> bool:
    current = int(time.time()) if now is None else now
    observed_seconds = projection.observed_at // 1_000_000
    return bool(
        projection.expires_at > current
        and observed_seconds <= current + TYPING_FUTURE_SKEW_SECONDS
        and projection.expires_at <= current + TYPING_TTL_SECONDS + TYPING_FUTURE_SKEW_SECONDS
    )


async def accept_typing_generation(
    redis: Redis,
    projection: TypingProjection,
    *,
    batch_index: int | None = None,
) -> bool:
    base_key = (
        "federation:typing:"
        f"{projection.channel_domain}:{projection.channel_id}:"
        f"{projection.user_domain}:{projection.user_id}"
    )
    result = await cast(Any, redis.eval)(
        ACCEPT_TYPING_GENERATION_SCRIPT,
        2,
        base_key,
        f"{base_key}:batch:{batch_index if batch_index is not None else 'local'}",
        str(projection.observed_at),
        str(TYPING_TTL_SECONDS + TYPING_FUTURE_SKEW_SECONDS),
    )
    return int(result or 0) == 1


def gateway_typing_payload(projection: TypingProjection) -> dict[str, object]:
    return {
        "channel_id": projection.channel_id,
        "channel_domain": projection.channel_domain,
        "user_id": projection.user_id,
        "user_domain": projection.user_domain,
        "timestamp": projection.observed_at // 1_000_000,
    }


async def typing_recipient_refs_by_domain(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
) -> dict[str, list[str]]:
    recipients: defaultdict[str, list[str]] = defaultdict(list)
    if channel.guild_id is not None and channel.guild_domain is not None:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is None or guild.unavailable:
            return {}
        users = list(
            await session.scalars(
                select(User)
                .join(
                    GuildMember,
                    (GuildMember.user_id == User.id)
                    & (GuildMember.user_domain == User.origin_domain),
                )
                .where(
                    GuildMember.guild_id == channel.guild_id,
                    GuildMember.guild_domain == channel.guild_domain,
                    User.disabled_at.is_(None),
                )
                .order_by(User.origin_domain, User.id)
            )
        )
        for user in users:
            try:
                permissions, _member = await calculate_permissions(
                    session,
                    guild,
                    user,
                    channel=channel,
                )
            except HTTPException:
                continue
            if permissions & Permission.VIEW_CHANNEL:
                recipients[user.origin_domain].append(f"{user.id}@{user.origin_domain}")
        return dict(recipients)
    users = list(
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
                User.disabled_at.is_(None),
            )
            .order_by(User.origin_domain, User.id)
        )
    )
    for user in users:
        recipients[user.origin_domain].append(f"{user.id}@{user.origin_domain}")
    return dict(recipients)


async def typing_destination_domains(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
) -> set[str]:
    """Compatibility helper returning only exact authorized remote homes."""

    return {
        domain
        for domain in await typing_recipient_refs_by_domain(session, settings, channel)
        if domain != settings.domain
    }


async def validate_typing_relay_scope(
    session: AsyncSession,
    settings: Settings,
    projection: TypingRelayRequest,
    *,
    authority_domain: str,
) -> tuple[Channel, User, set[str]]:
    if projection.channel_domain != authority_domain or authority_domain == settings.domain:
        raise ValueError("typing relay is not bound to a remote channel authority")
    if any(item.domain != settings.domain for item in projection.audience_user_refs):
        raise ValueError("typing relay audience targets another instance")
    channel = await session.get(Channel, (int(projection.channel_id), projection.channel_domain))
    actor = await session.get(User, (int(projection.user_id), projection.user_domain))
    if channel is None or channel.unavailable or actor is None:
        raise ValueError("typing relay references an unavailable room participant")
    if channel.guild_id is not None and channel.guild_domain is not None:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        actor_member = await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, actor.id, actor.origin_domain)
            if guild is not None
            else (-1, "invalid", -1, "invalid"),
        )
        if (
            guild is None
            or guild.unavailable
            or guild.origin_domain != authority_domain
            or actor_member is None
        ):
            raise ValueError("typing relay guild scope is invalid")
        try:
            permissions, _member = await calculate_permissions(
                session,
                guild,
                actor,
                channel=channel,
            )
        except HTTPException as exc:
            raise ValueError("typing relay guild actor is unauthorized") from exc
        if not permissions & Permission.VIEW_CHANNEL:
            raise ValueError("typing relay guild actor is unauthorized")
        recipients = set(
            (await typing_recipient_refs_by_domain(session, settings, channel)).get(
                settings.domain,
                [],
            )
        )
        admitted = recipients & {str(item) for item in projection.audience_user_refs}
        if not admitted:
            raise ValueError("typing relay has no current local recipient")
        return channel, actor, admitted
    conversation = await session.get(
        DMConversation,
        (channel.id, channel.origin_domain),
    )
    actor_participant = await session.get(
        DMParticipant,
        (channel.id, channel.origin_domain, actor.id, actor.origin_domain),
    )
    if (
        conversation is None
        or conversation.authority_domain != authority_domain
        or actor_participant is None
    ):
        raise ValueError("typing relay DM scope is invalid")
    recipients = set(
        (await typing_recipient_refs_by_domain(session, settings, channel)).get(
            settings.domain,
            [],
        )
    )
    admitted = recipients & {str(item) for item in projection.audience_user_refs}
    if not admitted:
        raise ValueError("typing relay has no current local recipient")
    return channel, actor, admitted


async def publish_local_typing(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    channel: Channel,
    projection: TypingProjection,
    *,
    audience_user_refs: set[str] | None = None,
) -> None:
    payload = gateway_typing_payload(projection)
    current = set(
        (await typing_recipient_refs_by_domain(session, settings, channel)).get(
            settings.domain,
            [],
        )
    )
    if audience_user_refs is not None:
        current &= audience_user_refs
    for user_ref in sorted(current):
        parsed = EntityRef(user_ref)
        await publish_ephemeral(
            redis,
            user_topic(settings.domain, parsed.id),
            "TYPING_START",
            payload,
        )


async def fanout_typing(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    projection: TypingProjection,
    audiences: dict[str, list[str]],
    *,
    guild_context: bool = False,
) -> None:
    """Best-effort direct relay; typing never enters a durable federation queue."""

    requests: list[tuple[str, TypingRelayRequest]] = []
    for domain, raw_audience in sorted(audiences.items()):
        if domain == settings.domain:
            continue
        audience = sorted(set(raw_audience))
        batch_count = (len(audience) + TYPING_AUDIENCE_BATCH_SIZE - 1) // TYPING_AUDIENCE_BATCH_SIZE
        if not audience or batch_count > TYPING_MAX_AUDIENCE_BATCHES:
            continue
        for batch_index in range(batch_count):
            start = batch_index * TYPING_AUDIENCE_BATCH_SIZE
            requests.append(
                (
                    domain,
                    TypingRelayRequest(
                        **projection.model_dump(mode="json"),
                        audience_user_refs=audience[start : start + TYPING_AUDIENCE_BATCH_SIZE],
                        batch_index=batch_index,
                        batch_count=batch_count,
                    ),
                )
            )

    async def deliver(domain: str, relay: TypingRelayRequest) -> None:
        try:
            async with sessionmaker() as delivery_session:
                response = await signed_request(
                    delivery_session,
                    settings,
                    "POST",
                    domain,
                    "/_kaede/v1/typing/relay",
                    payload=relay.model_dump(mode="json"),
                    request_timeout=2,
                    max_response_bytes=4096,
                    guild_context=guild_context,
                )
            if response.status_code not in {200, 204, 409}:
                log.info(
                    "federated_typing_rejected",
                    destination=domain,
                    status=response.status_code,
                )
        except (FederationNetworkError, RuntimeError):
            log.info("federated_typing_unavailable", destination=domain)

    for offset in range(0, len(requests), TYPING_RELAY_BATCH_SIZE):
        await asyncio.gather(
            *(
                deliver(domain, relay)
                for domain, relay in requests[offset : offset + TYPING_RELAY_BATCH_SIZE]
            )
        )


async def publish_authoritative_typing(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    channel: Channel,
    projection: TypingProjection,
) -> bool:
    if not typing_projection_is_fresh(projection) or not await accept_typing_generation(
        redis, projection
    ):
        return False
    audiences = await typing_recipient_refs_by_domain(session, settings, channel)
    await publish_local_typing(
        session,
        redis,
        settings,
        channel,
        projection,
        audience_user_refs=set(audiences.get(settings.domain, [])),
    )
    await fanout_typing(
        sessionmaker,
        settings,
        projection,
        audiences,
        guild_context=channel.guild_id is not None,
    )
    return True


__all__ = [
    "TypingProjection",
    "TypingPublishRequest",
    "TypingRelayRequest",
    "accept_typing_generation",
    "fanout_typing",
    "gateway_typing_payload",
    "new_typing_projection",
    "publish_authoritative_typing",
    "publish_local_typing",
    "typing_destination_domains",
    "typing_recipient_refs_by_domain",
    "typing_projection_is_fresh",
    "validate_typing_relay_scope",
]
