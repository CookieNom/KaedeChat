from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from livekit import api
from pydantic import Field
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import AdminPrincipal, require_admin
from app.api.dependencies import AuthenticatedUser, get_redis, get_session, require_user
from app.core.model_validation import UnambiguousInputModel
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import Settings, get_settings
from app.core.types import EntityRef
from app.db.models import RTCConfiguration, RTCRoomPlacement, RTCWebhookEvent, User
from app.federation.client import signed_request
from app.federation.network import decode_federation_response_json
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    require_guild_federation_access,
)
from app.voice.livekit import LiveKitControl, LiveKitError, receive_webhook
from app.voice.rtc import (
    WEBHOOK_PATH,
    RoutingHints,
    RTCConfig,
    configuration,
    provider_settings,
    room_settings,
    seal,
)

router = APIRouter(tags=["RTC"])


def ready(config: RTCConfig) -> None:
    if not config.api_key or not config.api_secret or not config.automatic_url:
        raise HTTPException(
            400,
            detail={"code": "RTC_CONFIGURATION_REQUIRED"},
        )


async def admin_payload(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    config = await configuration(session, settings)
    result = config.model_dump(mode="json", exclude={"api_secret"})
    result["secret_configured"] = config.api_secret is not None
    result["webhook_url"] = f"https://{settings.domain}{WEBHOOK_PATH}"
    result["webhook_path"] = WEBHOOK_PATH
    result["enabled_regions"] = [r.id for r in config.regions if r.enabled]
    result["routing_permissions"] = {
        "automatic": True,
        "manual": bool(result["enabled_regions"]),
        "require_hint": False,
    }
    result["webhooks"] = {
        "accepted": await session.scalar(
            select(func.count())
            .select_from(RTCWebhookEvent)
            .where(
                RTCWebhookEvent.domain == settings.domain,
            )
        ),
        "pending": await session.scalar(
            select(func.count())
            .select_from(RTCWebhookEvent)
            .where(
                RTCWebhookEvent.domain == settings.domain,
                RTCWebhookEvent.processed_at.is_(None),
            )
        ),
        "last_processed_at": await session.scalar(
            select(func.max(RTCWebhookEvent.processed_at)).where(
                RTCWebhookEvent.domain == settings.domain,
            )
        ),
    }
    return result


@router.get("/api/v1/administration/rtc")
async def get_rtc(
    principal: AdminPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    principal.require("rtc.manage")
    return await admin_payload(session, settings)


@router.put("/api/v1/administration/rtc")
async def save_rtc(
    payload: RTCConfig,
    principal: AdminPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    principal.require("rtc.manage")
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"rtc-config:{settings.domain}", 0),
            )
        )
    )
    previous = await configuration(session, settings)
    if payload.api_secret is None and payload.api_key == previous.api_key:
        payload.api_secret = previous.api_secret
    if payload.provider == "cinnamon":
        ready(payload)
    row = await session.get(RTCConfiguration, settings.domain)
    if row is None:
        row = RTCConfiguration(domain=settings.domain)
        session.add(row)
    row.configuration = payload.model_dump(mode="json", exclude={"api_secret", "api_key"})
    row.credentials = seal(
        settings,
        {
            "api_key": payload.api_key,
            "api_secret": payload.api_secret.get_secret_value() if payload.api_secret else None,
        },
    )
    await session.commit()
    return await admin_payload(session, settings)


async def discover(config: RTCConfig) -> list[dict[str, str]]:
    ready(config)
    if config.api_secret is None:
        raise HTTPException(400, detail={"code": "RTC_CONFIGURATION_REQUIRED"})
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                "https://" + config.automatic_url.removeprefix("wss://") + "/v1/rtc/probes",
                auth=(config.api_key, config.api_secret.get_secret_value()),
            )
            response.raise_for_status()
            data = response.json()
        enabled = {r.id for r in config.regions if r.enabled}
        targets = []
        for item in data["regions"][:64]:
            url = urlsplit(item["probe_url"])
            if (
                item["region"] in enabled
                and url.scheme == "https"
                and url.hostname
                and url.port in {None, 443}
                and not url.username
                and not url.password
                and url.path == "/rtc-probe"
                and not url.query
                and not url.fragment
            ):
                targets.append({"region": item["region"], "probe_url": item["probe_url"]})
        return targets
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            502,
            detail={"code": "RTC_PROBE_DISCOVERY_FAILED"},
        ) from exc


async def routing_payload(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    user: User,
    *,
    admin: bool = False,
) -> dict[str, Any]:
    await enforce_keyed_rate_limit(
        redis,
        Response(),
        ClientRateLimit("rtc-discovery", 30, 60),
        identity=f"{user.origin_domain}:{user.id}",
    )
    config = await configuration(session, settings)
    result: dict[str, Any] = {
        "provider": config.provider,
        "default_region": config.default_region,
        "allow_region_selection": config.allow_region_selection,
        "regions": [],
        "probes": [],
        "probe_ticket": None,
    }
    if config.provider == "builtin" and not admin:
        return result
    result["regions"] = [{"id": r.id, "name": r.name} for r in config.regions if r.enabled]
    try:
        result["probes"] = await discover(config)
    except HTTPException:
        if admin:
            raise
        return result
    ticket = secrets.token_urlsafe(24)
    await redis.set(
        f"rtc:probes:{settings.domain}:{user.origin_domain}:{user.id}:{ticket}",
        json.dumps([r["region"] for r in result["probes"]]),
        ex=60,
    )
    result["probe_ticket"] = ticket
    return result


class RTCDiscoveryRequest(UnambiguousInputModel):
    actor_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    channel_ref: EntityRef | None = None
    call_ref: EntityRef | None = None


async def discovery_authority(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    user: User,
    channel_ref: EntityRef | None,
    call_ref: EntityRef | None,
) -> str:
    from app.chat.permissions import get_permissions
    from app.core.permissions import Permission
    from app.voice.rooms import participant_identity
    from app.voice.service import load_voice_channel
    from app.voice.state import get_call

    if call_ref is not None:
        call_id, authority = call_ref.resolve(settings.domain)
        record = await get_call(redis, authority, call_id)
        if not record or participant_identity(user.id, user.origin_domain) not in record.get(
            "participants", []
        ):
            raise HTTPException(404, detail={"code": "CALL_NOT_FOUND"})
        return authority
    if channel_ref is not None:
        channel_id, domain = channel_ref.resolve(settings.domain)
        channel, guild = await load_voice_channel(session, channel_id, domain)
        permissions = await get_permissions(session, redis, guild, user, channel=channel)
        if not permissions & Permission.CONNECT:
            raise HTTPException(403, detail={"code": "VOICE_DENIED"})
        return guild.origin_domain
    return settings.domain


@router.get("/api/v1/voice/rtc")
async def client_rtc(
    channel_ref: EntityRef | None = None,
    call_ref: EntityRef | None = None,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    authority = await discovery_authority(
        session, redis, settings, auth.user, channel_ref, call_ref
    )
    if authority != settings.domain:
        response = await signed_request(
            session,
            settings,
            "POST",
            authority,
            "/_kaede/v1/voice/rtc",
            payload={
                "actor_id": str(auth.user.id),
                "channel_ref": str(channel_ref) if channel_ref else None,
                "call_ref": str(call_ref) if call_ref else None,
            },
            request_timeout=8,
            max_response_bytes=32 * 1024,
        )
        if response.status_code != 200:
            raise HTTPException(502, detail={"code": "VOICE_HOME_UNREACHABLE"})
        body = decode_federation_response_json(response)
        if not isinstance(body, dict):
            raise HTTPException(502, detail={"code": "VOICE_HOME_INVALID_RESPONSE"})
        return body
    return await routing_payload(session, redis, settings, auth.user)


@router.post("/_kaede/v1/voice/rtc")
async def federated_rtc(
    payload: RTCDiscoveryRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "rtc-probes",
        capacity=60,
        refill_per_minute=60,
    )
    if payload.channel_ref is not None:
        require_guild_federation_access(principal)
    user = await session.get(User, (int(payload.actor_id), principal.origin))
    if user is None or user.disabled_at is not None or user.account_type != "human":
        raise HTTPException(403, detail={"code": "VOICE_DENIED"})
    authority = await discovery_authority(
        session, redis, settings, user, payload.channel_ref, payload.call_ref
    )
    if authority != settings.domain or (payload.channel_ref is None and payload.call_ref is None):
        raise HTTPException(403, detail={"code": "VOICE_NOT_HOME"})
    return await routing_payload(session, redis, settings, user)


@router.get("/api/v1/administration/rtc/probes")
async def admin_probes(
    principal: AdminPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    principal.require("rtc.manage")
    return await routing_payload(session, redis, settings, principal.user, admin=True)


async def fresh_routing(
    redis: Redis,
    settings: Settings,
    user_id: int,
    hints: RoutingHints | None,
    user_domain: str | None = None,
) -> RoutingHints | None:
    if hints is None:
        return None
    allowed = []
    user_domain = user_domain or settings.domain
    if hints.probe_ticket:
        raw = await redis.get(
            f"rtc:probes:{settings.domain}:{user_domain}:{user_id}:{hints.probe_ticket}"
        )
        if raw:
            allowed = json.loads(raw)
    return hints.model_copy(
        update={"latency": {key: value for key, value in hints.latency.items() if key in allowed}}
    )


@router.post("/api/v1/administration/rtc/test")
async def test_rtc(
    payload: RoutingHints,
    principal: AdminPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    principal.require("rtc.manage")
    config = await configuration(session, settings)
    ready(config)
    config.provider = "cinnamon"  # Test saved settings before enabling the provider.
    region = payload.region or "automatic"
    url = config.automatic_url
    if region != "automatic":
        entry = next((r for r in config.regions if r.id == region and r.enabled), None)
        if entry is None:
            raise HTTPException(400, detail={"code": "RTC_REGION_INVALID"})
        url = entry.url
    lock = f"rtc:test:{settings.domain}"
    if not await redis.set(lock, "1", nx=True, ex=60):
        raise HTTPException(409, detail={"code": "RTC_TEST_BUSY"})
    name = f"kaede-rtc-test-{secrets.token_hex(16)}"
    created = False
    failure = None
    cleaned = False
    latency: dict[str, float] | None = None
    try:
        resolved = provider_settings(settings, config, url)
        hints = await fresh_routing(redis, settings, principal.user.id, payload)
        if hints and region == "automatic":
            enabled = {r.id for r in config.regions if r.enabled}
            latency = {key: value for key, value in hints.latency.items() if key in enabled}
        async with LiveKitControl.direct_client(resolved, latency) as client:
            try:
                await client.room.create_room(api.CreateRoomRequest(name=name, empty_timeout=60))
                created = True
                listed = await client.room.list_rooms(api.ListRoomsRequest(names=[name]))
                if not listed.rooms:
                    failure = "Room creation succeeded but the room could not be read back."
            finally:
                # The request may have succeeded remotely even if its response timed out.
                try:
                    await client.room.delete_room(api.DeleteRoomRequest(room=name))
                    cleaned = True
                except Exception:
                    failure = "Test-room cleanup could not be confirmed; remove only " + name
    except Exception:
        failure = failure or (
            "Connection test failed. Check the endpoint and project key/secret, region allowlist, "
            "plan capacity, and automatic/manual routing permissions."
        )
    finally:
        await redis.delete(lock)
    return {
        "region": region,
        "ok": created and cleaned and failure is None,
        "room": name,
        "cleaned_up": cleaned,
        "latency_regions": list(latency or {}),
        "message": failure
        or (
            "Room created, verified, and removed. "
            + (
                "Client latency measurements sent for: " + ", ".join(latency)
                if latency
                else "Automatic routing without hints verified."
                if region == "automatic"
                else "Manual regional routing verified."
            )
        ),
    }


@router.post(WEBHOOK_PATH, status_code=204)
async def cinnamon_webhook(
    request: Request,
    authorization: str = Header(default="", alias="Authorization"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    from app.federation.security import bounded_request_body

    raw = await bounded_request_body(
        request, max_bytes=256 * 1024, too_large_code="VOICE_WEBHOOK_TOO_LARGE"
    )
    try:
        body = raw.decode("utf-8")
        config = await configuration(session, settings)
        candidates = []
        if config.api_key and config.api_secret:
            candidates.append(
                provider_settings(
                    settings,
                    config.model_copy(update={"provider": "cinnamon"}),
                    config.automatic_url,
                )
            )
        # An active call retains its signing credential after configuration changes.
        room = json.loads(body).get("room", {}).get("name", "")
        placement = await session.get(RTCRoomPlacement, (settings.domain, room))
        if placement and placement.provider == "cinnamon":
            candidates.append(await room_settings(settings, room))
        event = None
        for candidate in candidates:
            try:
                event = receive_webhook(candidate, body, authorization)
                break
            except LiveKitError:
                continue
        if event is None or not event.id or len(event.id) > 128:
            raise ValueError("Unverified event")
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as exc:
        raise HTTPException(401, detail={"code": "VOICE_WEBHOOK_INVALID"}) from exc
    digest = hashlib.sha256(raw).hexdigest()
    await session.execute(
        insert(RTCWebhookEvent)
        .values(
            domain=settings.domain,
            event_id=event.id,
            body=body,
            body_hash=digest,
        )
        .on_conflict_do_nothing()
    )
    existing = await session.get(RTCWebhookEvent, (settings.domain, event.id))
    if existing is None or existing.body_hash != digest:
        raise HTTPException(409, detail={"code": "RTC_WEBHOOK_ID_CONFLICT"})
    await session.commit()
    return Response(status_code=204)
