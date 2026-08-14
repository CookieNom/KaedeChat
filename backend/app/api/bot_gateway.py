from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import select
from starlette.requests import Request

from app.bots.auth import BotPrincipal, require_bot
from app.db.bot_models import BotInstallation
from app.db.models import Channel, Guild

router = APIRouter(tags=["bot gateway"])
IDENTIFY_TIMEOUT_SECONDS = 10
HEARTBEAT_INTERVAL_SECONDS = 30
SESSION_TTL_SECONDS = 90
SESSION_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
if current > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
return current
"""
RELEASE_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current <= 1 then
  redis.call('DEL', KEYS[1])
else
  redis.call('DECR', KEYS[1])
end
return 1
"""


def authentication_request(websocket: WebSocket, identify: dict[str, Any]) -> Request:
    token = identify.get("token")
    timestamp = identify.get("timestamp")
    nonce = identify.get("nonce")
    proof = identify.get("proof")
    if not all(isinstance(value, str) for value in (token, nonce, proof)) or not isinstance(
        timestamp, int
    ):
        raise ValueError("identify authentication fields are invalid")
    token = cast(str, token)
    nonce = cast(str, nonce)
    proof = cast(str, proof)
    headers = [
        (b"authorization", f"Bot {token}".encode()),
        (b"x-kaede-bot-timestamp", str(timestamp).encode()),
        (b"x-kaede-bot-nonce", nonce.encode()),
        (b"x-kaede-bot-proof", proof.encode()),
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/bots/gateway",
            "raw_path": b"/api/v1/bots/gateway",
            "query_string": b"",
            "headers": headers,
            "client": websocket.client,
            "server": websocket.scope.get("server"),
            "root_path": "",
            "app": websocket.app,
        }
    )


def event_intent(event_type: str) -> str:
    if event_type.startswith("MESSAGE_REACTION"):
        return "message_reactions"
    if event_type.startswith("MESSAGE"):
        return "guild_messages"
    if event_type.startswith("GUILD_MEMBER"):
        return "guild_members"
    if event_type.startswith("PRESENCE"):
        return "guild_presences"
    if event_type.startswith("VOICE"):
        return "voice_states"
    if event_type.startswith("INTERACTION"):
        return "interactions"
    return "guilds"


def filtered_event(
    principal: BotPrincipal,
    event: dict[str, Any],
    granted_intents: set[str],
    granted_scopes: set[str],
) -> dict[str, Any] | None:
    event_type = event.get("t")
    data = event.get("d")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        return None
    effective_intents = set(principal.intents).intersection(granted_intents)
    if event_intent(event_type) not in effective_intents:
        return None
    rendered = dict(data)
    can_receive_content = (
        "message_content" in effective_intents
        and "messages.content" in principal.scopes
        and "messages.content" in granted_scopes
    )
    if event_type.startswith("MESSAGE") and not can_receive_content:
        rendered["content"] = None
        rendered["attachments"] = []
        rendered["content_unavailable"] = True
    return {
        "op": 0,
        "t": event_type,
        "s": int(event.get("topic_seq", 0)),
        "d": rendered,
    }


def encrypted_message_event(
    event: dict[str, Any], encrypted_channels: set[tuple[int, str]]
) -> bool:
    event_type = event.get("t")
    data = event.get("d")
    if not isinstance(event_type, str) or not event_type.startswith("MESSAGE"):
        return False
    if not isinstance(data, dict):
        return True
    if data.get("e2ee") is not None:
        return True
    try:
        channel_ref = (int(data["channel_id"]), str(data["channel_domain"]))
    except (KeyError, TypeError, ValueError):
        return True
    return channel_ref in encrypted_channels


async def encrypted_guild_channels(session: Any, guild: Guild) -> set[tuple[int, str]]:
    rows = await session.execute(
        select(Channel.id, Channel.origin_domain).where(
            Channel.guild_id == guild.id,
            Channel.guild_domain == guild.origin_domain,
            Channel.unavailable.is_(False),
            Channel.encryption_mode == "e2ee",
        )
    )
    return {(int(channel_id), str(domain)) for channel_id, domain in rows}


async def replay_topic(
    websocket: WebSocket,
    redis: Redis,
    principal: BotPrincipal,
    topic: str,
    after_sequence: int,
    granted_intents: set[str],
    granted_scopes: set[str],
    sessionmaker: Any,
    visibility: Any,
    encrypted_channels: set[tuple[int, str]],
) -> None:
    # Reuse the user Gateway's durable ACL fence without initializing the
    # standalone Gateway service when this API router is imported.
    from app.gateway import event_visibility

    entries = await redis.xrange(f"dispatch:stream:{topic}", min="-", max="+", count=1000)
    if after_sequence > 0 and entries:
        first_fields = entries[0][1]
        first_encoded = first_fields.get("event") if isinstance(first_fields, dict) else None
        if isinstance(first_encoded, bytes):
            first_encoded = first_encoded.decode()
        if isinstance(first_encoded, str):
            first = json.loads(first_encoded)
            first_sequence = int(first.get("topic_seq", 0)) if isinstance(first, dict) else 0
            if first_sequence > after_sequence + 1:
                await websocket.send_json(
                    {
                        "op": 0,
                        "t": "GATEWAY_GAP",
                        "s": first_sequence,
                        "topic": topic,
                        "d": {
                            "after_sequence": after_sequence,
                            "available_from": first_sequence,
                            "resync_required": True,
                        },
                    }
                )
    for _, fields in entries:
        encoded = fields.get("event") if isinstance(fields, dict) else None
        if isinstance(encoded, bytes):
            encoded = encoded.decode()
        if not isinstance(encoded, str):
            continue
        raw = json.loads(encoded)
        if not isinstance(raw, dict) or int(raw.get("topic_seq", 0)) <= after_sequence:
            continue
        visible, _ = await event_visibility(
            sessionmaker, redis, principal.user, visibility, topic, raw
        )
        if not visible or encrypted_message_event(raw, encrypted_channels):
            continue
        event = filtered_event(principal, raw, granted_intents, granted_scopes)
        if event is not None:
            event["topic"] = topic
            await websocket.send_json(event)


@router.websocket("/api/v1/bots/gateway")
async def bot_gateway(websocket: WebSocket) -> None:
    from app.gateway import build_visibility_summary, event_visibility

    await websocket.accept()
    redis = cast(Redis, websocket.app.state.redis)
    principal: BotPrincipal | None = None
    session_key: str | None = None
    pubsub = redis.pubsub()
    try:
        await websocket.send_json(
            {"op": 10, "d": {"heartbeat_interval": HEARTBEAT_INTERVAL_SECONDS * 1000}}
        )
        async with asyncio.timeout(IDENTIFY_TIMEOUT_SECONDS):
            identify = await websocket.receive_json()
        if not isinstance(identify, dict) or identify.get("op") != 2:
            await websocket.close(code=4401, reason="identify required")
            return
        async with websocket.app.state.sessionmaker() as session:
            principal = await require_bot(
                authentication_request(websocket, identify),
                session,
                redis,
            )
            session_key = (
                f"bot:gateway:sessions:{principal.application.origin_domain}:{principal.worker.id}"
            )
            admitted = await cast(
                Awaitable[object],
                redis.eval(
                    SESSION_SCRIPT,
                    1,
                    session_key,
                    str(principal.worker.session_limit),
                    str(SESSION_TTL_SECONDS),
                ),
            )
            if not int(cast(int | str | bytes, admitted)):
                await websocket.close(code=4429, reason="session concurrency exceeded")
                return
            installations = list(
                await session.scalars(
                    select(BotInstallation).where(
                        BotInstallation.application_id == principal.application.id,
                        BotInstallation.application_domain == principal.application.origin_domain,
                        BotInstallation.status == "active",
                    )
                )
            )
            guilds = [
                guild
                for installation in installations
                if (
                    guild := await session.get(
                        Guild, (installation.guild_id, installation.guild_domain)
                    )
                )
                is not None
            ]
            encrypted_by_topic = {
                f"guild:{guild.origin_domain}:{guild.id}": await encrypted_guild_channels(
                    session, guild
                )
                for guild in guilds
            }
        visibility = await build_visibility_summary(
            websocket.app.state.sessionmaker, redis, principal.user, guilds
        )
        topic_grants = {
            f"guild:{installation.guild_domain}:{installation.guild_id}": (
                set(installation.granted_intents),
                set(installation.granted_scopes),
            )
            for installation in installations
            if set(installation.granted_intents).intersection(principal.intents)
        }
        topics = list(topic_grants)
        cursors = identify.get("cursors", {})
        if not isinstance(cursors, dict) or len(cursors) > 1000:
            await websocket.close(code=4400, reason="invalid resume cursors")
            return
        await websocket.send_json(
            {
                "op": 0,
                "t": "READY",
                "s": 0,
                "d": {
                    "application_ref": (
                        f"{principal.application.id}@{principal.application.origin_domain}"
                    ),
                    "worker_id": str(principal.worker.id),
                    "installations": [
                        {
                            "id": str(installation.id),
                            "guild_ref": (f"{installation.guild_id}@{installation.guild_domain}"),
                            "capability_revision": str(installation.grant_revision),
                        }
                        for installation in installations
                    ],
                },
            }
        )
        for topic in topics:
            cursor = cursors.get(topic, 0)
            await replay_topic(
                websocket,
                redis,
                principal,
                topic,
                int(cursor) if isinstance(cursor, int) else 0,
                topic_grants[topic][0],
                topic_grants[topic][1],
                websocket.app.state.sessionmaker,
                visibility,
                encrypted_by_topic.get(topic, set()),
            )
        if topics:
            await pubsub.subscribe(*(f"dispatch:{topic}" for topic in topics))
        last_heartbeat = time.monotonic()
        while True:
            if principal.token.expires_at.timestamp() <= time.time():
                await websocket.close(code=4009, reason="bot token expired; reconnect")
                return
            if time.monotonic() - last_heartbeat > HEARTBEAT_INTERVAL_SECONDS * 2:
                await websocket.close(code=4408, reason="heartbeat timeout")
                return
            if session_key is not None:
                await redis.expire(session_key, SESSION_TTL_SECONDS)
            incoming_task = asyncio.create_task(websocket.receive_json())
            pubsub_task = asyncio.create_task(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            )
            done, pending = await asyncio.wait(
                {incoming_task, pubsub_task},
                timeout=1.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if incoming_task in done:
                incoming = incoming_task.result()
                if isinstance(incoming, dict) and incoming.get("op") == 1:
                    last_heartbeat = time.monotonic()
                    await websocket.send_json({"op": 11})
                elif isinstance(incoming, dict) and incoming.get("op") == 6:
                    await websocket.send_json({"op": 11})
                else:
                    await websocket.close(code=4400, reason="unsupported gateway opcode")
                    return
            if pubsub_task in done:
                message = pubsub_task.result()
                if not isinstance(message, dict):
                    continue
                encoded = message.get("data")
                if isinstance(encoded, bytes):
                    encoded = encoded.decode()
                if not isinstance(encoded, str):
                    continue
                raw = json.loads(encoded)
                if not isinstance(raw, dict):
                    continue
                channel = message.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode()
                topic = (
                    channel.removeprefix("dispatch:")
                    if isinstance(channel, str) and channel.startswith("dispatch:")
                    else ""
                )
                if raw.get("t") in {"CHANNEL_CREATE", "CHANNEL_UPDATE", "CHANNEL_DELETE"}:
                    matching_guild = next(
                        (
                            guild
                            for guild in guilds
                            if topic == f"guild:{guild.origin_domain}:{guild.id}"
                        ),
                        None,
                    )
                    if matching_guild is not None:
                        async with websocket.app.state.sessionmaker() as session:
                            encrypted_by_topic[topic] = await encrypted_guild_channels(
                                session, matching_guild
                            )
                visible, _ = await event_visibility(
                    websocket.app.state.sessionmaker,
                    redis,
                    principal.user,
                    visibility,
                    topic,
                    raw,
                )
                if not visible or encrypted_message_event(
                    raw, encrypted_by_topic.get(topic, set())
                ):
                    continue
                granted_intents, granted_scopes = topic_grants.get(topic, (set(), set()))
                event = filtered_event(principal, raw, granted_intents, granted_scopes)
                if event is None:
                    continue
                if topic:
                    event["topic"] = topic
                await websocket.send_json(event)
    except (TimeoutError, ValueError):
        await websocket.close(code=4401, reason="authentication failed")
    except WebSocketDisconnect:
        pass
    finally:
        await cast(Any, pubsub).aclose()
        if session_key is not None:
            with suppress(Exception):
                await cast(Awaitable[object], redis.eval(RELEASE_SCRIPT, 1, session_key))
