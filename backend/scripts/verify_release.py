from __future__ import annotations

import asyncio
import secrets
import time

from fastapi import HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.chat.events import publish_dispatch
from app.core.cache_warmup import WARMUP_READY_KEY, warm_identify_cache, warmup_manifest
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import get_settings
from app.db.models import FederationEvent, FederationOutbox, User
from app.db.session import create_engine_and_sessionmaker
from app.federation.delivery import MAX_BATCH_EVENTS
from app.federation.events import build_envelope, queue_event
from app.gateway import USER_SESSION_LIMIT, claim_user_gateway_session, session_key

SUBSCRIBERS = 20
EVENTS = 200
DESTINATIONS = 20


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def verify_rate_limit(redis: Redis) -> None:
    key = "rate:client:upload-ticket:release.localhost:4242"
    await redis.delete(key)
    accepted = 0
    rejected = 0
    for _ in range(CLIENT_RATE_LIMITS["upload_ticket"].limit + 1):
        try:
            await enforce_client_rate_limit(
                redis,
                Response(),
                CLIENT_RATE_LIMITS["upload_ticket"],
                user_id=4242,
                user_domain="release.localhost",
            )
            accepted += 1
        except HTTPException as exc:
            require(exc.status_code == 429, "client limiter returned the wrong status")
            rejected += 1
    require(accepted == CLIENT_RATE_LIMITS["upload_ticket"].limit, "bucket capacity drifted")
    require(rejected == 1, "bucket did not reject excess traffic")


async def verify_shared_fanout(redis: Redis) -> None:
    topic = "guild:release.localhost:4242"
    stream = f"dispatch:stream:{topic}"
    await redis.delete(stream, f"dispatch:seq:{topic}")
    subscribers = [redis.pubsub() for _ in range(SUBSCRIBERS)]
    try:
        await asyncio.gather(
            *(subscriber.subscribe(f"dispatch:{topic}") for subscriber in subscribers)
        )
        started = time.monotonic()
        for index in range(EVENTS):
            await publish_dispatch(redis, topic, "LOAD_SMOKE", {"index": index})
        received = 0
        deadline = time.monotonic() + 15
        while received < SUBSCRIBERS * EVENTS and time.monotonic() < deadline:
            messages = await asyncio.gather(
                *(
                    subscriber.get_message(ignore_subscribe_messages=True, timeout=0.05)
                    for subscriber in subscribers
                )
            )
            received += sum(message is not None for message in messages)
        elapsed = time.monotonic() - started
        require(received == SUBSCRIBERS * EVENTS, "shared fanout lost an event")
        require(int(await redis.xlen(stream)) == EVENTS, "fanout wrote per-subscriber streams")
        require(elapsed < 15, "shared fanout exceeded the smoke-test deadline")
    finally:
        await asyncio.gather(
            *(subscriber.aclose() for subscriber in subscribers)  # type: ignore[no-untyped-call]
        )


async def verify_gateway_session_cap(redis: Redis) -> None:
    user = User(id=4243, origin_domain="release.localhost", username="sessionprobe")
    user_sessions_key = f"gateway:user-sessions:{user.origin_domain}:{user.id}"
    session_ids = [f"release-session-{index}" for index in range(USER_SESSION_LIMIT + 1)]
    await redis.delete(user_sessions_key, *(session_key(item) for item in session_ids))
    try:
        for session_id in session_ids[:USER_SESSION_LIMIT]:
            require(
                await claim_user_gateway_session(redis, user, session_id),
                "gateway rejected a session below the per-user cap",
            )
        require(
            not await claim_user_gateway_session(redis, user, session_ids[-1]),
            "gateway admitted a session above the per-user cap",
        )
        await redis.zadd(user_sessions_key, {session_ids[0]: 0})
        require(
            await claim_user_gateway_session(redis, user, session_ids[-1]),
            "gateway did not reclaim an expired session slot",
        )
    finally:
        await redis.delete(user_sessions_key, *(session_key(item) for item in session_ids))


async def verify_federation_amplification() -> None:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            actor = await session.get(User, (4242, settings.domain))
            if actor is None:
                actor = User(
                    id=4242,
                    origin_domain=settings.domain,
                    is_local=True,
                    username="releaseprobe",
                    display_name="Release Probe",
                    password_hash=secrets.token_urlsafe(32),
                    email="release-probe@example.invalid",
                )
                session.add(actor)
                await session.flush()
            envelope = await build_envelope(
                session, settings, "release.load-smoke", actor, {"probe": True}
            )
            for index in range(DESTINATIONS):
                await queue_event(
                    session,
                    settings,
                    f"peer-{index}.localhost",
                    envelope,
                    discover_destination=True,
                )
            await session.commit()
            event_count = await session.scalar(
                select(func.count(FederationEvent.event_id)).where(
                    FederationEvent.origin_domain == settings.domain,
                    FederationEvent.event_id == envelope["event_id"],
                )
            )
            outbox_count = await session.scalar(
                select(func.count(FederationOutbox.id)).where(
                    FederationOutbox.event_origin_domain == settings.domain,
                    FederationOutbox.event_id == envelope["event_id"],
                )
            )
            require(int(event_count or 0) == 1, "federation copied an envelope per peer")
            require(int(outbox_count or 0) == DESTINATIONS, "federation destination fanout drifted")
            require(MAX_BATCH_EVENTS == 100, "federation batch bound drifted")
    finally:
        await engine.dispose()


async def main() -> None:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    try:
        await redis.delete(WARMUP_READY_KEY)
        await warm_identify_cache(redis, sessionmaker, settings)
        manifest = await warmup_manifest(redis)
        require(manifest.get("completed_at", 0) > 0, "gateway cache warmup did not complete")
        await verify_rate_limit(redis)
        await verify_gateway_session_cap(redis)
        await verify_shared_fanout(redis)
    finally:
        await redis.aclose()
        await engine.dispose()
    await verify_federation_amplification()
    print("M6 release verification passed")


if __name__ == "__main__":
    asyncio.run(main())
