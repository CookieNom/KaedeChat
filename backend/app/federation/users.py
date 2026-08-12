from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.payloads import user_payload
from app.core.settings import Settings
from app.core.task_wake import enqueue_best_effort
from app.db.models import (
    Channel,
    DMParticipant,
    GuildMember,
    Instance,
    Message,
    Relationship,
    User,
)
from app.federation.client import signed_request
from app.federation.identity_storage import FederationIdentityQuotaExceeded
from app.federation.network import (
    FederationInstanceQuotaExceeded,
    FederationNetworkError,
    decode_federation_response_json,
    ensure_peer,
    normalize_domain,
)
from app.federation.replication import database_snowflake, upsert_remote_user
from app.federation.schemas import RemoteUserProfile
from app.federation.security import validated_event_envelope

REMOTE_PROFILE_FRESHNESS = timedelta(minutes=5)
PROFILE_BY_REF_CAPABILITY = "profile-by-ref/1"
PROFILE_BY_REF_EVENT = "user.profile"
UNRESOLVED_PROFILE_RETRY_AFTER = timedelta(minutes=5)
UNRESOLVED_PROFILE_BATCH_SIZE = 100
PROFILE_REFRESH_GUILD_TOPIC_LIMIT = 500
PROFILE_REFRESH_USER_TOPIC_LIMIT = 1_000
REQUESTER_LOOKUPS_PER_MINUTE = 30
TARGET_DOMAIN_LOOKUPS_PER_MINUTE = 120
TARGET_DOMAIN_REF_REFRESHES_PER_MINUTE = 120
LOOKUP_RATE_SCRIPT = """
local requester_count = redis.call('INCR', KEYS[1])
if requester_count == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
if requester_count > tonumber(ARGV[2]) then return {requester_count, -1} end
local domain_count = redis.call('INCR', KEYS[2])
if domain_count == 1 then redis.call('EXPIRE', KEYS[2], tonumber(ARGV[1])) end
return {requester_count, domain_count}
"""
TARGET_RATE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
return count
"""


async def unresolved_profile_refresh_candidates(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = UNRESOLVED_PROFILE_BATCH_SIZE,
    now: datetime | None = None,
) -> list[tuple[int, str]]:
    """Return a bounded oldest-first batch supported by each user's home.

    Capability gating is intentionally part of the SQL query. During a rolling
    upgrade, old homes therefore retain harmless placeholders without causing
    repeated 404s or making a guild-history import fail.
    """

    cutoff = (now or datetime.now(UTC)) - UNRESOLVED_PROFILE_RETRY_AFTER
    return list(
        (
            await session.execute(
                select(User.id, User.origin_domain)
                .join(Instance, Instance.domain == User.origin_domain)
                .where(
                    User.is_local.is_(False),
                    User.profile_resolved.is_(False),
                    User.origin_domain != settings.domain,
                    or_(User.updated_at <= cutoff, User.updated_at == User.created_at),
                    Instance.capabilities.contains([PROFILE_BY_REF_CAPABILITY]),
                )
                .order_by(User.updated_at, User.origin_domain, User.id)
                .limit(max(1, min(limit, UNRESOLVED_PROFILE_BATCH_SIZE)))
            )
        ).tuples()
    )


async def unresolved_profile_peer_candidates(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = 20,
) -> list[str]:
    """Find old peers that may gain profile lookup after a rolling upgrade."""

    return list(
        await session.scalars(
            select(User.origin_domain)
            .join(Instance, Instance.domain == User.origin_domain)
            .where(
                User.is_local.is_(False),
                User.profile_resolved.is_(False),
                User.origin_domain != settings.domain,
                ~Instance.capabilities.contains([PROFILE_BY_REF_CAPABILITY]),
            )
            .group_by(User.origin_domain, Instance.updated_at)
            .order_by(Instance.updated_at, User.origin_domain)
            .limit(max(1, min(limit, 20)))
        )
    )


async def discover_profile_by_ref_capability(
    session: AsyncSession,
    settings: Settings,
    domain: str,
) -> bool:
    """Refresh one legacy peer's trust document before capability gating.

    Opaque history identities can create an Instance row without a key or
    capability document. A slow, separately deduplicated sweep calls this
    helper so a peer that upgrades later can converge without requiring
    unrelated traffic. The signed profile request itself remains strictly
    gated on the newly discovered capability.
    """

    domain = normalize_domain(domain)
    if domain == settings.domain:
        return False
    cached = await session.get(Instance, domain)
    try:
        instance = await ensure_peer(session, settings, domain, force=True)
        supported = PROFILE_BY_REF_CAPABILITY in (instance.capabilities or [])
    except (FederationNetworkError, RuntimeError):
        # Offline and pre-capability peers are normal during rolling upgrades.
        # Return a nonterminal result so the caller can commit the rotation
        # timestamp and allow later domains into the next bounded batch.
        supported = False
    finally:
        # Rotate discovery fairly even when a legacy or offline peer cannot be
        # refreshed. Otherwise the first alphabetical batch could starve all
        # later domains forever.
        if cached is not None:
            cached.updated_at = datetime.now(UTC)
    return supported


async def _profile_refresh_topics(session: AsyncSession, user: User) -> list[str]:
    """Find a bounded set of local audiences holding this public profile."""

    member_guilds = select(
        GuildMember.guild_id.label("guild_id"),
        GuildMember.guild_domain.label("guild_domain"),
    ).where(GuildMember.user_id == user.id, GuildMember.user_domain == user.origin_domain)
    authored_guilds = (
        select(
            Channel.guild_id.label("guild_id"),
            Channel.guild_domain.label("guild_domain"),
        )
        .join(
            Message,
            (Message.channel_id == Channel.id) & (Message.channel_domain == Channel.origin_domain),
        )
        .where(
            Message.author_id == user.id,
            Message.author_domain == user.origin_domain,
            Channel.guild_id.is_not(None),
            Channel.guild_domain.is_not(None),
        )
    )
    guild_refs = (
        await session.execute(
            member_guilds.union(authored_guilds).limit(PROFILE_REFRESH_GUILD_TOPIC_LIMIT)
        )
    ).tuples()

    target_participant = aliased(DMParticipant)
    local_participant = aliased(DMParticipant)
    dm_viewers = (
        select(
            local_participant.user_id.label("user_id"),
            local_participant.user_domain.label("user_domain"),
        )
        .join(
            target_participant,
            (target_participant.conversation_id == local_participant.conversation_id)
            & (target_participant.conversation_domain == local_participant.conversation_domain),
        )
        .join(
            User,
            (User.id == local_participant.user_id)
            & (User.origin_domain == local_participant.user_domain),
        )
        .where(
            target_participant.user_id == user.id,
            target_participant.user_domain == user.origin_domain,
            User.is_local.is_(True),
        )
    )
    relationship_viewers = select(
        Relationship.user_id.label("user_id"),
        Relationship.user_domain.label("user_domain"),
    ).where(Relationship.target_id == user.id, Relationship.target_domain == user.origin_domain)
    user_refs = (
        await session.execute(
            dm_viewers.union(relationship_viewers).limit(PROFILE_REFRESH_USER_TOPIC_LIMIT)
        )
    ).tuples()

    topics = {
        *(guild_topic(domain, guild_id) for guild_id, domain in guild_refs),
        *(user_topic(domain, user_id) for user_id, domain in user_refs),
    }
    return sorted(topics)


async def refresh_remote_user_by_ref(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    user_id: int,
    domain: str,
) -> User | None:
    """Upgrade one placeholder using a signed proof from its exact home.

    The guild authority that introduced a reference never participates in this
    lookup. The requested composite ID, response actor, signed subject and
    profile must all match before any mutable field is accepted.
    """

    domain = normalize_domain(domain)
    if domain == settings.domain:
        return None
    user_id = database_snowflake(str(user_id), "profile user id")
    user = await session.get(User, (user_id, domain))
    if user is None or user.is_local or user.profile_resolved:
        return None
    instance = await session.get(Instance, domain)
    if instance is None or PROFILE_BY_REF_CAPABILITY not in (instance.capabilities or []):
        return None

    window = int(datetime.now(UTC).timestamp() // 60)
    target_count = await cast(Any, redis.eval)(
        TARGET_RATE_SCRIPT,
        1,
        f"federation:user-ref-refresh:rate:target:{domain}:{window}",
        "120",
    )
    if int(target_count) > TARGET_DOMAIN_REF_REFRESHES_PER_MINUTE:
        # Rotate this placeholder behind the rest of the oldest-first sweep so
        # one busy home cannot pin the same batch at the front indefinitely.
        user.updated_at = datetime.now(UTC)
        await session.commit()
        return None
    try:
        response = await signed_request(
            session,
            settings,
            "GET",
            domain,
            "/_kaede/v1/users/profile",
            query={"user_id": str(user_id), "user_domain": domain},
            request_timeout=5,
            max_response_bytes=64 * 1024,
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        user.updated_at = datetime.now(UTC)
        await session.commit()
        raise
    # Unsupported/deleted identities are an availability outcome, not a
    # history-import failure. Touch the placeholder so the sweep retries later
    # instead of creating a hot loop against a rolling-upgrade peer.
    if response.status_code in {404, 405, 410, 501}:
        user.updated_at = datetime.now(UTC)
        await session.commit()
        return None
    if response.status_code != 200:
        user.updated_at = datetime.now(UTC)
        await session.commit()
        raise FederationNetworkError("remote profile-by-reference refresh failed")
    try:
        raw_envelope = decode_federation_response_json(response)
        envelope = await validated_event_envelope(session, settings, domain, raw_envelope)
        if envelope.type != PROFILE_BY_REF_EVENT:
            raise ValueError("profile response has the wrong event type")
        expected_ref = (user_id, domain)
        if (int(envelope.actor.id), envelope.actor.domain) != expected_ref:
            raise ValueError("profile response actor does not match the requested identity")
        subject = envelope.content.get("subject")
        if not isinstance(subject, dict):
            raise ValueError("profile response subject is missing")
        subject_ref = (
            database_snowflake(subject.get("id"), "profile subject id"),
            normalize_domain(str(subject.get("origin_domain", ""))),
        )
        profile = RemoteUserProfile.model_validate(envelope.content.get("profile"))
        if subject_ref != expected_ref or (int(profile.id), profile.origin_domain) != expected_ref:
            raise ValueError("profile response does not match the requested identity")
    except (FederationNetworkError, TypeError, ValueError):
        user.updated_at = datetime.now(UTC)
        await session.commit()
        raise FederationNetworkError("remote profile-by-reference proof is invalid") from None

    # Recheck under a row lock after the network request. Another worker may
    # have resolved the same identity while this signed proof was in flight.
    current = await session.scalar(
        select(User)
        .where(User.id == user_id, User.origin_domain == domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is None or current.is_local or current.profile_resolved:
        await session.rollback()
        return None
    resolved = await upsert_remote_user(session, settings, profile)
    if not resolved.profile_resolved:
        # The authoritative home supplied a handle already owned by another
        # composite identity. Keep the opaque row and back off without turning
        # a malicious/equivocating profile into a failed snapshot or hot loop.
        resolved.updated_at = datetime.now(UTC)
        await session.commit()
        return None
    topics = await _profile_refresh_topics(session, resolved)
    await session.commit()
    payload = user_payload(resolved)
    for topic in topics:
        await publish_dispatch(redis, topic, "USER_UPDATE", payload)
    return resolved


def split_handle(handle: str) -> tuple[str, str]:
    username, separator, raw_domain = handle.strip().lower().rpartition("@")
    username = username.removeprefix("@")
    if not separator or not username:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    try:
        return username, normalize_domain(raw_domain)
    except FederationNetworkError:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"}) from None


async def resolve_handle(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    requester_key: str,
    handle: str,
) -> User:
    username, domain = split_handle(handle)
    user = await session.scalar(
        select(User).where(
            User.origin_domain == domain,
            func.lower(User.username) == username,
        )
    )
    if domain == settings.domain:
        if user is None or not user.is_local:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
        return user
    if user is not None and datetime.now(UTC) - user.updated_at <= REMOTE_PROFILE_FRESHNESS:
        return user
    if user is not None:
        refresh_key = f"federation:user-lookup:refresh:{domain}:{username}"
        if await redis.set(refresh_key, "1", ex=30, nx=True):
            # Lazy import avoids a task-registration cycle at module import time.
            from app.tasks import federation_user_refresh

            queued = await enqueue_best_effort(federation_user_refresh, username, domain)
            if not queued:
                await redis.delete(refresh_key)
        return user
    negative_key = f"federation:user-lookup:missing:{domain}:{username}"
    if await redis.exists(negative_key):
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    window = int(datetime.now(UTC).timestamp() // 60)
    counts = await cast(Any, redis.eval)(
        LOOKUP_RATE_SCRIPT,
        2,
        f"federation:user-lookup:rate:requester:{requester_key}:{window}",
        f"federation:user-lookup:rate:target:{domain}:{window}",
        "120",
        str(REQUESTER_LOOKUPS_PER_MINUTE),
    )
    requester_count, domain_count = (int(item) for item in counts)
    if (
        requester_count > REQUESTER_LOOKUPS_PER_MINUTE
        or domain_count > TARGET_DOMAIN_LOOKUPS_PER_MINUTE
    ):
        raise HTTPException(
            status_code=429,
            detail={"code": "FEDERATION_LOOKUP_RATE_LIMITED"},
            headers={"Retry-After": "60"},
        )
    try:
        response = await signed_request(
            session,
            settings,
            "GET",
            domain,
            "/_kaede/v1/users/lookup",
            query={"handle": f"{username}@{domain}"},
        )
    except FederationInstanceQuotaExceeded as exc:
        raise HTTPException(status_code=507, detail=exc.detail()) from exc
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        if user is not None:
            return user
        raise HTTPException(status_code=503, detail={"code": "FEDERATION_UNAVAILABLE"}) from None
    if response.status_code == 404:
        await redis.set(negative_key, "1", ex=60)
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail={"code": "FEDERATION_LOOKUP_FAILED"})
    try:
        profile = RemoteUserProfile.model_validate(decode_federation_response_json(response))
    except (FederationNetworkError, TypeError, ValueError):
        raise HTTPException(
            status_code=502, detail={"code": "FEDERATION_IDENTITY_MISMATCH"}
        ) from None
    if profile.origin_domain != domain or profile.username.lower() != username:
        raise HTTPException(status_code=502, detail={"code": "FEDERATION_IDENTITY_MISMATCH"})
    try:
        user = await upsert_remote_user(session, settings, profile)
    except FederationIdentityQuotaExceeded as exc:
        raise HTTPException(status_code=507, detail=exc.detail()) from exc
    except FederationInstanceQuotaExceeded as exc:
        raise HTTPException(status_code=507, detail=exc.detail()) from exc
    user.updated_at = datetime.now(UTC)
    await session.commit()
    return user


async def refresh_remote_user(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    username: str,
    domain: str,
) -> User | None:
    """Refresh one stale cached profile outside a request's latency path."""

    domain = normalize_domain(domain)
    if domain == settings.domain:
        return None
    window = int(datetime.now(UTC).timestamp() // 60)
    target_count = await cast(Any, redis.eval)(
        TARGET_RATE_SCRIPT,
        1,
        f"federation:user-lookup:rate:target:{domain}:{window}",
        "120",
    )
    if int(target_count) > TARGET_DOMAIN_LOOKUPS_PER_MINUTE:
        return None
    response = await signed_request(
        session,
        settings,
        "GET",
        domain,
        "/_kaede/v1/users/lookup",
        query={"handle": f"{username}@{domain}"},
    )
    if response.status_code == 404:
        await redis.set(f"federation:user-lookup:missing:{domain}:{username}", "1", ex=60)
        return None
    if response.status_code != 200:
        raise FederationNetworkError("remote profile refresh failed")
    try:
        profile = RemoteUserProfile.model_validate(decode_federation_response_json(response))
    except (FederationNetworkError, TypeError, ValueError):
        raise FederationNetworkError("remote profile refresh returned invalid identity") from None
    if profile.origin_domain != domain or profile.username.lower() != username:
        raise FederationNetworkError("remote profile refresh returned mismatched identity")
    user = await upsert_remote_user(session, settings, profile)
    user.updated_at = datetime.now(UTC)
    await session.commit()
    return user
