from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import select
from starlette.requests import Request

from app.bots.auth import BotPrincipal, require_bot
from app.bots.installations import installation_has_membership
from app.chat.events import interaction_dispatch_audience
from app.db.bot_models import BotApplication, BotInstallation, BotToken, BotWorker
from app.db.models import Channel, Guild, User

router = APIRouter(tags=["bot gateway"])
IDENTIFY_TIMEOUT_SECONDS = 10
HEARTBEAT_INTERVAL_SECONDS = 30
SESSION_TTL_SECONDS = 90
AUTHORIZATION_RECHECK_SECONDS = 1.0
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


@dataclass(frozen=True, slots=True)
class GatewayAuthorizationState:
    fingerprint: tuple[object, ...]
    installations: tuple[BotInstallation, ...]


def gateway_authorization_fingerprint(
    application: BotApplication,
    worker: BotWorker,
    token: BotToken,
    installations: list[BotInstallation] | tuple[BotInstallation, ...],
) -> tuple[object, ...]:
    """Capture every persisted grant that can authorize a Gateway event."""

    installation_grants = tuple(
        sorted(
            (
                installation.id,
                installation.application_id,
                installation.application_domain,
                installation.guild_id,
                installation.guild_domain,
                installation.bot_user_id,
                installation.bot_user_domain,
                installation.status,
                installation.revoked_at,
                installation.grant_revision,
                tuple(sorted(installation.granted_scopes)),
                tuple(sorted(installation.granted_intents)),
                installation.granted_permissions,
                tuple(sorted(installation.channel_restrictions)),
                installation.e2ee_mode,
            )
            for installation in installations
        )
    )
    return (
        application.id,
        application.origin_domain,
        application.bot_user_id,
        application.bot_user_domain,
        application.status,
        application.manifest_generation,
        application.revocation_generation,
        tuple(sorted(application.default_scopes)),
        tuple(sorted(application.default_intents)),
        worker.id,
        worker.application_id,
        worker.application_domain,
        worker.generation,
        worker.revoked_at,
        worker.expires_at,
        tuple(sorted(worker.scopes)),
        tuple(sorted(worker.intents)),
        tuple(sorted(worker.target_domains)),
        worker.session_limit,
        token.id,
        token.application_id,
        token.application_domain,
        token.worker_id,
        token.revoked_at,
        token.expires_at,
        token.dpop_thumbprint,
        tuple(sorted(token.scopes)),
        tuple(sorted(token.intents)),
        installation_grants,
    )


async def current_gateway_authorization(
    session: Any,
    principal: BotPrincipal,
) -> GatewayAuthorizationState | None:
    """Reload and validate the complete current authorization for a connection."""

    row = (
        await session.execute(
            select(BotToken, BotWorker, BotApplication, User)
            .join(BotWorker, BotWorker.id == BotToken.worker_id)
            .join(
                BotApplication,
                (BotApplication.id == BotToken.application_id)
                & (BotApplication.origin_domain == BotToken.application_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotToken.id == principal.token.id,
                BotToken.worker_id == principal.worker.id,
                BotToken.application_id == principal.application.id,
                BotToken.application_domain == principal.application.origin_domain,
                User.account_type == "bot",
                User.disabled_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return None
    token, worker, application, user = row
    now = datetime.now(UTC)
    if (
        token.revoked_at is not None
        or token.expires_at <= now
        or worker.revoked_at is not None
        or (worker.expires_at is not None and worker.expires_at <= now)
        or application.status != "active"
        or user.account_type != "bot"
        or user.disabled_at is not None
        or (worker.application_id, worker.application_domain)
        != (application.id, application.origin_domain)
        or (application.bot_user_id, application.bot_user_domain) != (user.id, user.origin_domain)
        or (user.id, user.origin_domain) != (principal.user.id, principal.user.origin_domain)
    ):
        return None
    current_scopes = (
        set(token.scopes).intersection(worker.scopes).intersection(application.default_scopes)
    )
    current_intents = (
        set(token.intents).intersection(worker.intents).intersection(application.default_intents)
    )
    if not set(principal.scopes).issubset(current_scopes) or not set(principal.intents).issubset(
        current_intents
    ):
        return None
    installations = tuple(
        await session.scalars(
            select(BotInstallation)
            .where(
                BotInstallation.application_id == application.id,
                BotInstallation.application_domain == application.origin_domain,
                BotInstallation.bot_user_id == principal.user.id,
                BotInstallation.bot_user_domain == principal.user.origin_domain,
                BotInstallation.status == "active",
                installation_has_membership(),
            )
            .order_by(BotInstallation.id)
        )
    )
    if not installations:
        return None
    return GatewayAuthorizationState(
        gateway_authorization_fingerprint(application, worker, token, installations),
        installations,
    )


@dataclass(slots=True)
class GatewayAuthorizationGuard:
    sessionmaker: Any
    principal: BotPrincipal
    expected_fingerprint: tuple[object, ...]
    last_checked: float = 0.0

    async def current(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self.last_checked < AUTHORIZATION_RECHECK_SECONDS:
            return True
        self.last_checked = now
        async with self.sessionmaker() as session:
            state = await current_gateway_authorization(session, self.principal)
        return state is not None and state.fingerprint == self.expected_fingerprint


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
    if event_type == "ATTACHMENT_UPDATE":
        return "guild_messages"
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
    if event_type.startswith("TYPING"):
        return "guild_typing"
    if event_type.startswith("INTERACTION"):
        return "interactions"
    return "guilds"


def event_scope(event_type: str) -> str:
    """Return the data grant required to receive an event category."""

    if event_type == "ATTACHMENT_UPDATE":
        return "attachments.read"
    if event_type.startswith("MESSAGE_REACTION"):
        return "reactions.read"
    if event_type.startswith("MESSAGE"):
        return "messages.metadata"
    if event_type.startswith("GUILD_MEMBER") or event_type.startswith("PRESENCE"):
        return "members.read"
    if event_type.startswith("VOICE"):
        return "voice.states.read"
    if event_type.startswith("CHANNEL") or event_type.startswith("TYPING"):
        return "channels.read"
    if event_type.startswith("GUILD_ROLE"):
        return "roles.read"
    if event_type.startswith("INTERACTION"):
        return "applications.commands"
    return "guilds.read"


def normalized_bot_event_type(event_type: str, data: dict[str, Any]) -> str:
    """Translate shared client projections into stable bot event contracts.

    Human clients intentionally receive compact ``MESSAGE_UPDATE`` projections
    for reactions and pins.  Bots need distinct event names so the
    ``message_reactions`` intent works independently from ``guild_messages``
    and sparse payloads are never mistaken for complete Message resources.
    """

    if event_type == "MESSAGE_UPDATE" and isinstance(data.get("reaction"), str):
        return "MESSAGE_REACTION_REMOVE" if data.get("removed") is True else "MESSAGE_REACTION_ADD"
    if event_type == "MESSAGE_UPDATE" and isinstance(data.get("pinned"), bool):
        return "MESSAGE_PIN_UPDATE"
    return event_type


def guild_context_from_topic(topic: str | None) -> tuple[int, str] | None:
    """Return the authoritative guild context encoded by a subscribed topic."""

    if not isinstance(topic, str) or not topic.startswith("guild:"):
        return None
    parts = topic.split(":", 2)
    if len(parts) != 3:
        return None
    _, domain, raw_id = parts
    try:
        guild_id = int(raw_id)
    except ValueError:
        return None
    if guild_id < 0 or not domain:
        return None
    return guild_id, domain


def filtered_event(
    principal: BotPrincipal,
    event: dict[str, Any],
    granted_intents: set[str],
    granted_scopes: set[str],
    *,
    topic: str | None = None,
    installation_id: int | None = None,
) -> dict[str, Any] | None:
    event_type = event.get("t")
    data = event.get("d")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        return None
    event_type = normalized_bot_event_type(event_type, data)
    if event_type.startswith("INTERACTION"):
        if event_type == "INTERACTION_CREATE" and interaction_dispatch_audience(event) != (
            f"{principal.user.id}@{principal.user.origin_domain}"
        ):
            return None
        if data.get("application_ref") != (
            f"{principal.application.id}@{principal.application.origin_domain}"
        ):
            return None
        if installation_id is None or data.get("installation_id") != str(installation_id):
            return None
    effective_intents = set(principal.intents).intersection(granted_intents)
    if event_intent(event_type) not in effective_intents:
        return None
    required_scope = event_scope(event_type)
    if required_scope not in principal.scopes or required_scope not in granted_scopes:
        return None
    rendered = dict(data)
    if guild_context := guild_context_from_topic(topic):
        guild_id, guild_domain = guild_context
        # The subscribed topic is the ACL boundary. Project its canonical
        # context into sparse presence/voice/member events rather than trusting
        # an optional producer field that may be absent or stale.
        rendered["guild_id"] = str(guild_id)
        rendered["guild_domain"] = guild_domain
        if event_type.startswith("VOICE") and rendered.get("channel_id") is not None:
            rendered["channel_domain"] = guild_domain
    can_receive_content = (
        "message_content" in effective_intents
        and "messages.content" in principal.scopes
        and "messages.content" in granted_scopes
    )
    if event_type.startswith("MESSAGE") and "content" in rendered and not can_receive_content:
        rendered["content"] = None
        rendered["content_unavailable"] = True
    can_receive_attachments = (
        "attachments.read" in principal.scopes and "attachments.read" in granted_scopes
    )
    if (
        event_type.startswith("MESSAGE")
        and "attachments" in rendered
        and not can_receive_attachments
    ):
        rendered["attachments"] = []
        rendered["attachments_unavailable"] = True
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
    installation_id: int,
    sessionmaker: Any,
    visibility: Any,
    encrypted_channels: set[tuple[int, str]],
    authorization_guard: GatewayAuthorizationGuard,
) -> bool:
    # Reuse the user Gateway's durable ACL fence without initializing the
    # standalone Gateway service when this API router is imported.
    from app.gateway import event_visibility

    if not await authorization_guard.current(force=True):
        return False
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
        event = filtered_event(
            principal,
            raw,
            granted_intents,
            granted_scopes,
            topic=topic,
            installation_id=installation_id,
        )
        if event is not None:
            # A replay can contain many events. Recheck immediately before each
            # disclosure so a concurrent suspension or grant reduction cannot
            # drain an already-materialized replay under the old snapshot.
            if not await authorization_guard.current(force=True):
                return False
            event["topic"] = topic
            await websocket.send_json(event)
    return True


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
            requested_intents = identify.get("intents", list(principal.intents))
            if (
                not isinstance(requested_intents, list)
                or len(requested_intents) > 32
                or any(not isinstance(item, str) for item in requested_intents)
                or len(set(requested_intents)) != len(requested_intents)
            ):
                await websocket.close(code=4403, reason="invalid gateway intents")
                return
            principal = BotPrincipal(
                principal.user,
                principal.application,
                principal.worker,
                principal.token,
                principal.scopes,
                frozenset(requested_intents).intersection(principal.intents),
            )
            authorization = await current_gateway_authorization(session, principal)
            if authorization is None:
                await websocket.close(code=4009, reason="bot authorization changed; reconnect")
                return
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
            installations = list(authorization.installations)
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
        authorization_guard = GatewayAuthorizationGuard(
            websocket.app.state.sessionmaker,
            principal,
            authorization.fingerprint,
        )
        visibility = await build_visibility_summary(
            websocket.app.state.sessionmaker, redis, principal.user, guilds
        )
        topic_grants = {
            f"guild:{installation.guild_domain}:{installation.guild_id}": (
                set(installation.granted_intents),
                set(installation.granted_scopes),
                installation.id,
            )
            for installation in installations
            if set(installation.granted_intents).intersection(principal.intents)
        }
        topics = list(topic_grants)
        cursors = identify.get("cursors", {})
        if not isinstance(cursors, dict) or len(cursors) > 1000:
            await websocket.close(code=4400, reason="invalid resume cursors")
            return
        if not await authorization_guard.current(force=True):
            await websocket.close(code=4009, reason="bot authorization changed; reconnect")
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
                    "intents": sorted(principal.intents),
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
            replay_current = await replay_topic(
                websocket,
                redis,
                principal,
                topic,
                int(cursor) if isinstance(cursor, int) else 0,
                topic_grants[topic][0],
                topic_grants[topic][1],
                topic_grants[topic][2],
                websocket.app.state.sessionmaker,
                visibility,
                encrypted_by_topic.get(topic, set()),
                authorization_guard,
            )
            if not replay_current:
                await websocket.close(code=4009, reason="bot authorization changed; reconnect")
                return
        if topics:
            await pubsub.subscribe(*(f"dispatch:{topic}" for topic in topics))
        last_heartbeat = time.monotonic()
        while True:
            if not await authorization_guard.current():
                await websocket.close(code=4009, reason="bot authorization changed; reconnect")
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
                granted_intents, granted_scopes, installation_id = topic_grants.get(
                    topic, (set(), set(), None)
                )
                event = filtered_event(
                    principal,
                    raw,
                    granted_intents,
                    granted_scopes,
                    topic=topic,
                    installation_id=installation_id,
                )
                if event is None:
                    continue
                if not await authorization_guard.current(force=True):
                    await websocket.close(code=4009, reason="bot authorization changed; reconnect")
                    return
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
