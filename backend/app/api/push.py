from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedUser, get_redis, get_session, require_user
from app.auth.security import encrypt_secret
from app.chat.payloads import public_user_display_name
from app.chat.permissions import get_permissions
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.task_wake import enqueue_best_effort
from app.db.models import (
    Attachment,
    Channel,
    DMParticipant,
    Guild,
    GuildNotificationSetting,
    Message,
    PushDevice,
    PushRelayDelivery,
    PushRelaySubscription,
    ReadState,
    User,
    UserSettings,
)
from app.federation.network import FederationNetworkError, normalize_domain
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    federation_client_ip,
    matching_block,
)
from app.push.client import revoke_relay_subscription as revoke_remote_relay_subscription
from app.push.presentation import (
    notification_previews_enabled,
    push_body,
    push_presentation,
)
from app.push.relay import (
    RELAY_GRANT_TTL_SECONDS,
    RELAY_SUBSCRIPTION_DAYS,
    encrypt_provider_token,
    encrypt_wake_secret,
    opaque_token,
    secret_digest,
    signed_push_document,
    subscription_id,
    utc_from_epoch,
    verify_push_document,
)
from app.push.schemas import (
    PushDeviceCreate,
    PushDeviceResponse,
    PushNotificationRedeem,
    PushNotificationResponse,
    PushRelayEnrollmentComplete,
    PushRelayEnrollmentCreate,
    PushRelaySubscriptionCreate,
    PushRelayWakeCreate,
)
from app.push.service import PUSH_TOKEN_CONTEXT
from app.push.sync import claim_push_sync, load_push_sync

router = APIRouter(prefix="/api/v1/users/@me/push-devices", tags=["push devices"])
relay_router = APIRouter(tags=["push relay"])


def require_relay_transport_host(request: Request, settings: Settings) -> None:
    """Keep relay-only endpoints off the ordinary application origin."""

    if request.url.hostname != urlsplit(settings.push_relay_url).hostname:
        raise HTTPException(status_code=404, detail={"code": "PUSH_RELAY_NOT_FOUND"})


def rotated_token_fields(
    body: PushDeviceCreate,
    *,
    digest: bytes,
    encrypted: bytes,
    now: datetime,
) -> dict[str, Any]:
    """Return the fields that must change together when a provider token rotates."""

    return {
        "platform": body.platform,
        "transport": "direct_fcm",
        "token_hash": digest,
        "token_encrypted": encrypted,
        "relay_origin": None,
        "relay_subscription_id": None,
        "relay_route_id": None,
        "relay_wake_secret_encrypted": None,
        "device_name": body.device_name,
        "enabled": True,
        "last_seen_at": now,
        "updated_at": now,
    }


def payload(device: PushDevice) -> PushDeviceResponse:
    return PushDeviceResponse(
        id=device.id,
        platform=cast(Literal["android", "ios"], device.platform),
        device_name=device.device_name,
        enabled=device.enabled,
        last_seen_at=device.last_seen_at.isoformat(),
        transport=cast(Literal["relay", "direct_fcm"], device.transport),
        relay_origin=device.relay_origin,
    )


def _push_event_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "PUSH_EVENT_NOT_FOUND",
            "message": "This notification is no longer available",
        },
    )


def relay_enrollment_key(route_id: str) -> str:
    digest = hashlib.sha256(route_id.encode("ascii")).hexdigest()
    return f"push:relay-enrollment:{digest}"


async def _push_relay_rate_limit(
    redis: Redis,
    scope: str,
    *,
    limit: int,
) -> None:
    digest = hashlib.sha256(scope.encode()).hexdigest()[:32]
    key = f"push-relay:rate:{digest}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail={"code": "PUSH_RELAY_RATE_LIMITED", "retry_after": 60},
            headers={"Retry-After": "60"},
        )


async def _claim_or_not_found(redis: Redis, token: str, encoded: str | bytes) -> None:
    if not await claim_push_sync(redis, token, encoded):
        raise _push_event_not_found()


async def _suppress_push(redis: Redis, token: str, encoded: str | bytes) -> Response:
    await _claim_or_not_found(redis, token, encoded)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("")
async def list_push_devices(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[PushDeviceResponse]:
    devices = list(
        await session.scalars(
            select(PushDevice)
            .where(
                PushDevice.user_id == auth.user.id,
                PushDevice.user_domain == auth.user.origin_domain,
            )
            .order_by(PushDevice.last_seen_at.desc())
        )
    )
    return [payload(device) for device in devices]


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_push_device(
    body: PushDeviceCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PushDeviceResponse:
    if not settings.push_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "PUSH_DISABLED", "message": "Mobile push is disabled"},
        )
    digest = hashlib.sha256(body.token.encode("utf-8")).digest()
    now = datetime.now(UTC)
    encrypted = encrypt_secret(
        body.token,
        settings.secret_key_bytes,
        context=PUSH_TOKEN_CONTEXT,
    )
    device_id = str(body.installation_id)

    # A provider token can rotate and an installation can change accounts. Lock
    # both identities in a deterministic order so concurrent refresh/login
    # callbacks cannot leave the same physical installation attached to two
    # users or violate the token uniqueness constraint.
    lock_names = sorted((f"installation:{device_id}", f"push-token:{digest.hex()}"))
    for lock_name in lock_names:
        await session.scalar(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_name, 0)))
        )
    await session.execute(
        delete(PushDevice).where(
            PushDevice.token_hash == digest,
            PushDevice.id != device_id,
        )
    )
    statement = pg_insert(PushDevice).values(
        id=device_id,
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
        user_is_local=True,
        platform=body.platform,
        transport="direct_fcm",
        token_hash=digest,
        token_encrypted=encrypted,
        device_name=body.device_name,
        enabled=True,
        last_seen_at=now,
    )
    device_id = str(
        await session.scalar(
            statement.on_conflict_do_update(
                index_elements=[PushDevice.id],
                set_={
                    "user_id": auth.user.id,
                    "user_domain": auth.user.origin_domain,
                    "user_is_local": True,
                    # Provider tokens rotate over the lifetime of one app
                    # installation.  Keep the lookup digest paired with the
                    # encrypted token or a later registration could collide
                    # with a stale digest and invalid-token cleanup would act
                    # on the wrong credential.
                    **rotated_token_fields(
                        body,
                        digest=digest,
                        encrypted=encrypted,
                        now=now,
                    ),
                },
            ).returning(PushDevice.id)
        )
    )
    await session.commit()
    device = await session.get(PushDevice, device_id)
    if device is None:  # pragma: no cover - defensive against external deletion
        raise RuntimeError("push registration disappeared after commit")
    return payload(device)


@router.get("/capabilities")
async def push_capabilities(
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return {
        "relay": {
            "enabled": settings.push_relay_enabled,
            "url": settings.push_relay_url if settings.push_relay_enabled else None,
            "origin": settings.push_relay_origin if settings.push_relay_enabled else None,
            "app_id": settings.push_relay_app_id if settings.push_relay_enabled else None,
            "privacy": {
                "content_free": True,
                "relay_sees_home_origin": True,
                "relay_sees_delivery_timing": True,
            },
        },
        "direct_fcm": settings.push_enabled,
    }


@router.post("/relay/enrollment")
async def begin_relay_enrollment(
    body: PushRelayEnrollmentCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not settings.push_relay_enabled:
        raise HTTPException(
            status_code=503,
            detail={"code": "PUSH_RELAY_DISABLED", "message": "Push relay is disabled"},
        )
    if body.app_id != settings.push_relay_app_id:
        raise HTTPException(status_code=400, detail={"code": "PUSH_RELAY_APP_MISMATCH"})
    await _push_relay_rate_limit(
        redis,
        f"enrollment-user:{auth.user.origin_domain}:{auth.user.id}",
        limit=10,
    )
    pending = json.dumps(
        {
            "user_id": str(auth.user.id),
            "user_domain": auth.user.origin_domain,
            "installation_id": str(body.installation_id),
            "platform": body.platform,
            "app_id": body.app_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    if not await redis.set(
        relay_enrollment_key(body.route_id),
        pending,
        ex=RELAY_GRANT_TTL_SECONDS,
        nx=True,
    ):
        raise HTTPException(status_code=409, detail={"code": "PUSH_RELAY_ENROLLMENT_EXISTS"})
    now = int(time.time())
    grant = await signed_push_document(
        session,
        settings,
        {
            "type": "push.enrollment",
            "grant_id": opaque_token(),
            "audience": settings.push_relay_origin,
            "app_id": body.app_id,
            "platform": body.platform,
            "route_id": body.route_id,
            "issued_at": now,
            "expires_at": now + RELAY_GRANT_TTL_SECONDS,
        },
    )
    return {
        "relay_url": settings.push_relay_url,
        "relay_origin": settings.push_relay_origin,
        "grant": grant,
    }


@router.post("/relay/complete", status_code=status.HTTP_201_CREATED)
async def complete_relay_enrollment(
    body: PushRelayEnrollmentComplete,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> PushDeviceResponse:
    if not settings.push_relay_enabled:
        raise HTTPException(status_code=503, detail={"code": "PUSH_RELAY_DISABLED"})
    try:
        receipt = dict(body.receipt)
        await verify_push_document(
            session,
            settings,
            receipt,
            expected_origin=settings.push_relay_origin,
            expected_type="push.subscription",
        )
        subscription = str(receipt["subscription_id"])
        if (
            re.fullmatch(r"^kps_[A-Za-z0-9_-]{32,59}$", subscription) is None
            or receipt.get("home_origin") != settings.domain
            or receipt.get("route_id") != body.route_id
            or receipt.get("app_id") != settings.push_relay_app_id
            or receipt.get("platform") != body.platform
        ):
            raise ValueError("push subscription receipt is not bound to this enrollment")
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PUSH_RELAY_RECEIPT_INVALID",
                "message": "The relay receipt is invalid or expired.",
            },
        ) from None
    encoded_pending = await redis.getdel(relay_enrollment_key(body.route_id))
    expected_pending = {
        "user_id": str(auth.user.id),
        "user_domain": auth.user.origin_domain,
        "installation_id": str(body.installation_id),
        "platform": body.platform,
        "app_id": settings.push_relay_app_id,
    }
    try:
        pending = json.loads(encoded_pending) if encoded_pending is not None else None
    except (TypeError, ValueError):
        pending = None
    if pending != expected_pending:
        raise HTTPException(
            status_code=400,
            detail={"code": "PUSH_RELAY_ENROLLMENT_EXPIRED"},
        )
    device_id = str(body.installation_id)
    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"installation:{device_id}", 0)))
    )
    await session.execute(
        delete(PushDevice).where(
            PushDevice.relay_subscription_id == subscription,
            PushDevice.id != device_id,
        )
    )
    now = datetime.now(UTC)
    statement = pg_insert(PushDevice).values(
        id=device_id,
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
        user_is_local=True,
        platform=body.platform,
        transport="relay",
        token_hash=None,
        token_encrypted=None,
        relay_origin=settings.push_relay_origin,
        relay_subscription_id=subscription,
        relay_route_id=body.route_id,
        relay_wake_secret_encrypted=encrypt_wake_secret(body.wake_secret, settings),
        device_name=body.device_name,
        enabled=True,
        last_seen_at=now,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[PushDevice.id],
            set_={
                "user_id": auth.user.id,
                "user_domain": auth.user.origin_domain,
                "user_is_local": True,
                "platform": body.platform,
                "transport": "relay",
                "token_hash": None,
                "token_encrypted": None,
                "relay_origin": settings.push_relay_origin,
                "relay_subscription_id": subscription,
                "relay_route_id": body.route_id,
                "relay_wake_secret_encrypted": encrypt_wake_secret(body.wake_secret, settings),
                "device_name": body.device_name,
                "enabled": True,
                "last_seen_at": now,
                "updated_at": now,
            },
        )
    )
    await session.commit()
    device = await session.get(PushDevice, device_id)
    if device is None:
        raise RuntimeError("relay push registration disappeared after commit")
    return payload(device)


async def _relay_registration_rate_limit(
    redis: Redis, request: Request, settings: Settings
) -> None:
    source = federation_client_ip(request, settings)
    await _push_relay_rate_limit(redis, f"registration-source:{source}", limit=30)


@relay_router.post("/push/v1/subscriptions", status_code=status.HTTP_201_CREATED)
async def create_relay_subscription(
    body: PushRelaySubscriptionCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not settings.push_relay_service_enabled:
        raise HTTPException(status_code=404, detail={"code": "PUSH_RELAY_NOT_FOUND"})
    require_relay_transport_host(request, settings)
    await _relay_registration_rate_limit(redis, request, settings)
    grant = dict(body.grant)
    try:
        home_origin = normalize_domain(str(grant["origin"]))
        block = await matching_block(session, home_origin)
        if block is not None and block.level == "suspend":
            raise ValueError("push enrollment authority is blocked")
        await verify_push_document(
            session,
            settings,
            grant,
            expected_origin=home_origin,
            expected_type="push.enrollment",
        )
        grant_id = str(grant["grant_id"])
        app_id = str(grant["app_id"])
        platform = str(grant["platform"])
        route_id = str(grant["route_id"])
        if (
            re.fullmatch(r"^[A-Za-z0-9_-]{43}$", grant_id) is None
            or re.fullmatch(r"^[A-Za-z0-9_-]{43}$", route_id) is None
            or platform not in {"android", "ios"}
            or grant.get("audience") != settings.domain
            or app_id != settings.push_relay_app_id
        ):
            raise ValueError("push grant has the wrong audience or application")
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PUSH_RELAY_GRANT_INVALID",
                "message": "The signed enrollment grant is invalid or expired.",
            },
        ) from None
    await _push_relay_rate_limit(
        redis,
        f"registration-origin:{home_origin}",
        limit=300,
    )
    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"push-relay-grant:{grant_id}", 0)))
    )
    if await session.scalar(
        select(PushRelaySubscription.id).where(PushRelaySubscription.grant_id == grant_id)
    ):
        raise HTTPException(status_code=409, detail={"code": "PUSH_RELAY_GRANT_USED"})
    token_hash = hashlib.sha256(body.provider_token.encode()).digest()
    identifier = subscription_id()
    expiry = datetime.now(UTC) + timedelta(days=RELAY_SUBSCRIPTION_DAYS)
    session.add(
        PushRelaySubscription(
            id=identifier,
            grant_id=grant_id,
            home_origin=home_origin,
            app_id=app_id,
            platform=platform,
            route_id=route_id,
            provider_token_hash=token_hash,
            provider_token_encrypted=encrypt_provider_token(body.provider_token, settings),
            management_secret_hash=secret_digest(body.management_secret),
            enabled=True,
            expires_at=expiry,
        )
    )
    now = int(time.time())
    receipt = await signed_push_document(
        session,
        settings,
        {
            "type": "push.subscription",
            "subscription_id": identifier,
            "home_origin": home_origin,
            "app_id": app_id,
            "platform": platform,
            "route_id": route_id,
            "issued_at": now,
            "expires_at": now + RELAY_GRANT_TTL_SECONDS,
        },
    )
    await session.commit()
    return {"subscription_id": identifier, "receipt": receipt}


@relay_router.delete(
    "/push/v1/subscriptions/{subscription}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_relay_subscription_by_device(
    subscription: str,
    request: Request,
    management_secret: str = Header(alias="X-Kaede-Push-Management", min_length=43, max_length=43),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> None:
    """Allow the device to revoke its opaque route without contacting its home."""

    if not settings.push_relay_service_enabled:
        raise HTTPException(status_code=404, detail={"code": "PUSH_RELAY_NOT_FOUND"})
    require_relay_transport_host(request, settings)
    await _relay_registration_rate_limit(redis, request, settings)
    if re.fullmatch(r"^kps_[A-Za-z0-9_-]{32,59}$", subscription) is None:
        raise HTTPException(status_code=404, detail={"code": "PUSH_RELAY_NOT_FOUND"})
    row = await session.get(PushRelaySubscription, subscription)
    if row is None or not secrets.compare_digest(
        row.management_secret_hash, secret_digest(management_secret)
    ):
        # Do not reveal whether a guessed subscription exists.
        raise HTTPException(status_code=404, detail={"code": "PUSH_RELAY_NOT_FOUND"})
    row.enabled = False
    await session.commit()


@relay_router.post("/_kaede/push/v1/wakes", status_code=status.HTTP_202_ACCEPTED)
async def accept_relay_wake(
    body: PushRelayWakeCreate,
    request: Request,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    if not settings.push_relay_service_enabled:
        raise HTTPException(status_code=404, detail={"code": "PUSH_RELAY_NOT_FOUND"})
    require_relay_transport_host(request, settings)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "push-relay-wake",
        capacity=600,
        refill_per_minute=600,
    )
    if body.expires_at <= int(time.time()) or body.expires_at > int(time.time()) + 600:
        raise HTTPException(status_code=400, detail={"code": "PUSH_RELAY_WAKE_EXPIRED"})
    subscription = await session.get(PushRelaySubscription, body.subscription_id)
    if (
        subscription is None
        or not subscription.enabled
        or subscription.expires_at <= datetime.now(UTC)
        or subscription.home_origin != principal.origin
        or subscription.route_id != body.route_id
    ):
        raise HTTPException(status_code=410, detail={"code": "PUSH_RELAY_SUBSCRIPTION_GONE"})
    subscription.last_seen_at = datetime.now(UTC)
    inserted = await session.scalar(
        pg_insert(PushRelayDelivery)
        .values(
            home_origin=principal.origin,
            request_id=body.request_id,
            subscription_id=body.subscription_id,
            route_id=body.route_id,
            event_token=body.event_token,
            delivery_id=body.delivery_id,
            wake_mac=body.wake_mac,
            priority=body.priority,
            expires_at=utc_from_epoch(body.expires_at),
        )
        .on_conflict_do_nothing(
            index_elements=[PushRelayDelivery.home_origin, PushRelayDelivery.request_id]
        )
        .returning(PushRelayDelivery.request_id)
    )
    await session.commit()
    if inserted is None:
        existing = await session.get(PushRelayDelivery, (principal.origin, body.request_id))
        expected = (
            body.subscription_id,
            body.route_id,
            body.event_token,
            body.delivery_id,
            body.expires_at,
            body.priority,
            body.wake_mac,
        )
        if existing is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "PUSH_RELAY_IDEMPOTENCY_CONFLICT"},
            )
        actual = (
            existing.subscription_id,
            existing.route_id,
            existing.event_token,
            existing.delivery_id,
            int(existing.expires_at.timestamp()),
            existing.priority,
            existing.wake_mac,
        )
        if actual != expected:
            raise HTTPException(
                status_code=409,
                detail={"code": "PUSH_RELAY_IDEMPOTENCY_CONFLICT"},
            )
        return {"status": existing.state}
    # The minute sweep is the durable recovery path; this wake keeps normal
    # delivery latency low without making relay acceptance depend on Taskiq.
    from app.tasks import push_relay_provider_sweep

    await enqueue_best_effort(push_relay_provider_sweep)
    return {"status": "pending"}


@relay_router.delete(
    "/_kaede/push/v1/subscriptions/{subscription}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_relay_subscription(
    subscription: str,
    request: Request,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.push_relay_service_enabled:
        raise HTTPException(status_code=404, detail={"code": "PUSH_RELAY_NOT_FOUND"})
    require_relay_transport_host(request, settings)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "push-relay-revoke",
        capacity=120,
        refill_per_minute=120,
    )
    row = await session.get(PushRelaySubscription, subscription)
    if row is not None and row.home_origin == principal.origin:
        row.enabled = False
        await session.commit()


@router.post(
    "/notifications/redeem",
    response_model=PushNotificationResponse,
    responses={status.HTTP_204_NO_CONTENT: {"description": "Notification suppressed"}},
)
async def redeem_push_notification(
    body: PushNotificationRedeem,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> PushNotificationResponse | Response:
    """Redeem one opaque, short-lived wake token over the authenticated API."""

    loaded = await load_push_sync(redis, body.event_token)
    if loaded is None:
        raise _push_event_not_found()
    encoded, event = loaded
    if (
        event.device_id != str(body.installation_id)
        or event.user_id != auth.user.id
        or event.user_domain != auth.user.origin_domain
    ):
        raise _push_event_not_found()

    device = await session.scalar(
        select(PushDevice).where(
            PushDevice.id == event.device_id,
            PushDevice.user_id == auth.user.id,
            PushDevice.user_domain == auth.user.origin_domain,
            PushDevice.enabled.is_(True),
        )
    )
    if device is None:
        raise _push_event_not_found()

    if event.kind in {"call", "moderation", "relationship"}:
        preferences_row = await session.get(
            UserSettings,
            (auth.user.id, auth.user.origin_domain),
        )
        preferences = preferences_row.notification_settings if preferences_row is not None else {}
        if preferences.get("presence_preference") == "dnd":
            return await _suppress_push(redis, body.event_token, encoded)
        if not event.title or not event.body or not event.event_ref:
            return await _suppress_push(redis, body.event_token, encoded)
        await _claim_or_not_found(redis, body.event_token, encoded)
        return PushNotificationResponse(
            kind=cast(Literal["call", "moderation", "relationship"], event.kind),
            title=event.title[:160],
            body=event.body[:500],
            channel_ref=event.channel_ref,
            event_ref=event.event_ref,
            sent_at=event.sent_at or datetime.now(UTC).isoformat(),
        )

    message = await session.get(Message, (event.message_id, event.message_domain))
    if (
        message is None
        or message.deleted_at is not None
        or (message.author_id == auth.user.id and message.author_domain == auth.user.origin_domain)
    ):
        return await _suppress_push(redis, body.event_token, encoded)
    channel = await session.get(Channel, (message.channel_id, message.channel_domain))
    if channel is None:
        return await _suppress_push(redis, body.event_token, encoded)

    preferences_row = await session.get(
        UserSettings,
        (auth.user.id, auth.user.origin_domain),
    )
    preferences = preferences_row.notification_settings if preferences_row is not None else {}
    if preferences.get("presence_preference") == "dnd":
        return await _suppress_push(redis, body.event_token, encoded)

    is_dm = channel.type == 1
    is_mention = event.kind == "mention"
    guild: Guild | None = None
    if is_dm:
        participant = await session.scalar(
            select(DMParticipant.user_id).where(
                DMParticipant.conversation_id == channel.id,
                DMParticipant.conversation_domain == channel.origin_domain,
                DMParticipant.user_id == auth.user.id,
                DMParticipant.user_domain == auth.user.origin_domain,
            )
        )
        if participant is None or event.kind != "direct_message":
            return await _suppress_push(redis, body.event_token, encoded)
        if not bool(preferences.get("direct_messages", True)):
            return await _suppress_push(redis, body.event_token, encoded)
    elif channel.guild_id is not None and channel.guild_domain is not None:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is None or guild.unavailable:
            return await _suppress_push(redis, body.event_token, encoded)
        permissions = await get_permissions(session, redis, guild, auth.user, channel=channel)
        if not permissions & Permission.VIEW_CHANNEL:
            return await _suppress_push(redis, body.event_token, encoded)
        notification_setting = await session.get(
            GuildNotificationSetting,
            (auth.user.id, auth.user.origin_domain, guild.id, guild.origin_domain),
        )
        level = notification_setting.level if notification_setting is not None else "mentions"
        if level == "none" or (level == "mentions" and not is_mention):
            return await _suppress_push(redis, body.event_token, encoded)
        if is_mention and not bool(preferences.get("mentions", True)):
            return await _suppress_push(redis, body.event_token, encoded)
        if event.kind not in {"mention", "guild_message"}:
            return await _suppress_push(redis, body.event_token, encoded)
    else:
        return await _suppress_push(redis, body.event_token, encoded)

    read_state = await session.get(
        ReadState,
        (auth.user.id, auth.user.origin_domain, channel.id, channel.origin_domain),
    )
    if read_state is not None and read_state.last_message_id is not None:
        last_read = (read_state.last_message_id, read_state.last_message_domain or "")
        if last_read >= (message.id, message.origin_domain):
            return await _suppress_push(redis, body.event_token, encoded)

    author = await session.get(User, (message.author_id, message.author_domain))
    if author is None:
        return await _suppress_push(redis, body.event_token, encoded)
    has_attachment = bool(
        await session.scalar(
            select(
                exists().where(
                    Attachment.message_id == message.id,
                    Attachment.message_domain == message.origin_domain,
                    Attachment.deleted_at.is_(None),
                )
            )
        )
    )
    author_name = message.webhook_name or public_user_display_name(author)
    avatar_hash = (
        message.webhook_avatar_hash if message.webhook_id is not None else author.avatar_hash
    )
    if avatar_hash is not None and (
        len(avatar_hash) != 64
        or any(character not in "0123456789abcdef" for character in avatar_hash)
    ):
        avatar_hash = None
    if is_dm:
        title = author_name
    else:
        if guild is None:  # Defensive: guild channels were validated above.
            return await _suppress_push(redis, body.event_token, encoded)
        title = f"{author_name} in {guild.name}"
    encrypted = message.e2ee is not None
    show_preview = notification_previews_enabled(preferences) and not encrypted
    if encrypted:
        title, notification_body = "Kaede Chat", "New encrypted message"
    else:
        title, notification_body = push_presentation(
            show_preview=show_preview,
            is_dm=is_dm,
            is_mention=is_mention,
            title=title,
            body=push_body(message, has_attachment),
        )
    await _claim_or_not_found(redis, body.event_token, encoded)
    return PushNotificationResponse(
        kind=cast(Literal["direct_message", "mention", "guild_message"], event.kind),
        title=title,
        body=notification_body,
        channel_ref=f"{channel.id}@{channel.origin_domain}",
        message_ref=f"{message.id}@{message.origin_domain}",
        sender_name=author_name if show_preview else None,
        sender_ref=(f"{author.id}@{author.origin_domain}" if show_preview else None),
        sender_avatar_hash=avatar_hash if show_preview else None,
        sent_at=message.created_at.isoformat(),
    )


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_push_device(
    device_id: UUID,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    device = await session.scalar(
        select(PushDevice)
        .where(
            PushDevice.id == str(device_id),
            PushDevice.user_id == auth.user.id,
            PushDevice.user_domain == auth.user.origin_domain,
        )
        .with_for_update()
    )
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PUSH_DEVICE_NOT_FOUND", "message": "Push device not found"},
        )
    relay_subscription = device.relay_subscription_id if device.transport == "relay" else None
    await session.delete(device)
    await session.commit()
    if relay_subscription is not None:
        with suppress(FederationNetworkError):
            await revoke_remote_relay_subscription(session, settings, relay_subscription)
