"""Durable, retryable Cinnamon events; reconcile stale deliveries before effects."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import Response
from google.protobuf.json_format import Parse
from livekit import api
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.db.models import RTCWebhookEvent
from app.voice.livekit import LiveKitControl, LiveKitError


async def process_pending_webhooks(
    factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
) -> None:
    from app.api.voice import process_livekit_event

    # One bounded batch per coordinator cycle. Row locks fence concurrent workers;
    # a process crash rolls back the acknowledgement and leaves the event retryable.
    for _ in range(20):
        async with factory() as session:
            row = await session.scalar(
                select(RTCWebhookEvent)
                .where(
                    RTCWebhookEvent.domain == settings.domain,
                    RTCWebhookEvent.processed_at.is_(None),
                    RTCWebhookEvent.retry_at <= datetime.now(UTC),
                )
                .order_by(RTCWebhookEvent.accepted_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return
            event_id = row.event_id
            attempts = row.attempts + 1
            try:
                event = Parse(row.body, api.WebhookEvent(), ignore_unknown_fields=True)
                room = event.room.name
                stale = False
                control = LiveKitControl(settings)
                from app.voice.rooms import parse_room_name

                try:
                    parse_room_name(room)
                    managed_room = True
                except ValueError:
                    managed_room = False
                if managed_room and event.event in {"participant_joined", "participant_left"}:
                    try:
                        participants = await control.list_participants(room)
                    except LiveKitError as exc:
                        if getattr(exc.__cause__, "code", None) != "not_found":
                            raise
                        participants = []
                    live = next(
                        (p for p in participants if p.identity == event.participant.identity), None
                    )
                    if event.event == "participant_joined":
                        stale = live is None or live.sid != event.participant.sid
                    else:
                        stale = live is not None

                async def completed() -> Response:
                    return Response(status_code=204)

                if not stale:
                    await process_livekit_event(event, session, redis, settings, completed)
                row.processed_at = datetime.now(UTC)
                row.attempts = attempts
                await session.commit()
            except Exception:
                await session.rollback()
                row = await session.get(RTCWebhookEvent, (settings.domain, event_id))
                if row is not None:
                    row.attempts = attempts
                    row.retry_at = datetime.now(UTC) + timedelta(
                        seconds=min(3600, 2 ** min(attempts, 12))
                    )
                    await session.commit()


async def webhook_worker(
    factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
) -> None:
    while True:
        try:
            await process_pending_webhooks(factory, redis, settings)
        except Exception:
            structlog.get_logger().exception("rtc_webhook_worker_failed")
        await asyncio.sleep(2)
