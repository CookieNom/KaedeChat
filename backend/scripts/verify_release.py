from __future__ import annotations

import asyncio
import time

from fastapi import HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.auth.security import hash_password
from app.chat.events import publish_dispatch
from app.core.cache_warmup import WARMUP_READY_KEY, warm_identify_cache, warmup_manifest
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import get_settings
from app.db.models import FederationEvent, FederationOutbox, User
from app.db.session import create_engine_and_sessionmaker
from app.federation.delivery import MAX_BATCH_EVENTS
from app.federation.events import build_envelope, queue_event
from app.gateway import USER_SESSION_LIMIT, claim_user_gateway_session, session_key
from scripts.verification import VerificationFailure, failure_message, require

SUBSCRIBERS = 20
EVENTS = 200
DESTINATIONS = 20
RELEASE_PASSWORD_SECRET = "A" * 43
RELEASE_AUTH_SALT = bytes(range(16))
RELEASE_VAULT_SALT = bytes(reversed(range(16)))


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
            require(
                exc.status_code == 429,
                f"client limiter expected HTTP 429; received HTTP {exc.status_code}",
            )
            rejected += 1
    require(
        accepted == CLIENT_RATE_LIMITS["upload_ticket"].limit,
        "rate-limit bucket accepted an unexpected number of requests: expected "
        f"{CLIENT_RATE_LIMITS['upload_ticket'].limit}, received {accepted}",
    )
    require(rejected == 1, f"rate-limit bucket expected 1 rejection; received {rejected}")


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
        expected_received = SUBSCRIBERS * EVENTS
        require(
            received == expected_received,
            f"shared fanout expected {expected_received} deliveries; received {received}",
        )
        stream_length = int(await redis.xlen(stream))
        require(
            stream_length == EVENTS,
            f"shared fanout stream expected {EVENTS} events; received {stream_length}",
        )
        require(
            elapsed < 15,
            f"shared fanout deadline is 15 seconds; elapsed {elapsed:.2f} seconds",
        )
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
                    password_hash=hash_password(RELEASE_PASSWORD_SECRET),
                    password_kdf_version=2,
                    password_auth_salt=RELEASE_AUTH_SALT,
                    e2ee_vault_salt=RELEASE_VAULT_SALT,
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
            require(
                int(event_count or 0) == 1,
                f"federation expected 1 stored envelope; received {int(event_count or 0)}",
            )
            require(
                int(outbox_count or 0) == DESTINATIONS,
                f"federation expected {DESTINATIONS} outbox destinations; "
                f"received {int(outbox_count or 0)}",
            )
            require(
                MAX_BATCH_EVENTS == 100,
                f"federation batch bound expected 100; received {MAX_BATCH_EVENTS}",
            )
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
    try:
        asyncio.run(main())
    except VerificationFailure as error:
        raise SystemExit(failure_message("release", error, "make release-check")) from None
