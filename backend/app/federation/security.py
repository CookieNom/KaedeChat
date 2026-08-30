from __future__ import annotations

import base64
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, HTTPException, Request, WebSocket
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_redis, get_session
from app.chat.dm_mutations import authority_attested_dm_message_mutation
from app.chat.forwarding import authority_attested_forward_source
from app.chat.poll_results import (
    authority_attested_direct_poll_result,
    authority_attested_dm_poll_mutation,
)
from app.core.federation import (
    BLOCK_POLICY_ADVISORY_NAME,
    SigningInput,
    authority_attested_group_event_ref,
    canonical_request_target,
    content_sha256,
    federation_policy_holds_event,
    guild_authority_event_ref,
    guild_crosspost_authority_event_ref,
    guild_media_delete_request_ref,
    guild_message_authority_event_refs,
    terminal_room_event_ref,
    verify_envelope,
    verify_request,
)
from app.core.json_limits import strict_json_loads
from app.core.permissions import PERMISSION_SCHEMA_CAPABILITY
from app.core.proxy import resolve_client_ip
from app.core.settings import Settings, get_settings
from app.db.models import Instance, InstanceBlock, PeerKey
from app.federation.network import (
    FederationNetworkError,
    ensure_peer,
    normalize_domain,
    peer_key_needs_refresh,
)
from app.federation.schemas import KEY_ID_RE, EventEnvelope

AUTHORIZATION_RE = re.compile(
    r'^Kaede origin="(?P<origin>[^"]+)",key="(?P<key>[^"]+)",sig="(?P<sig>[^"]+)"$'
)
REQUEST_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
MAX_FEDERATION_REQUEST_BYTES = 1024 * 1024
RATE_LIMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill_per_ms = tonumber(ARGV[3])
local cost = tonumber(ARGV[4]) or 1
local values = redis.call('HMGET', key, 'tokens', 'updated')
local tokens = tonumber(values[1]) or capacity
local updated = tonumber(values[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - updated) * refill_per_ms)
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'updated', now)
redis.call('PEXPIRE', key, math.ceil(capacity / refill_per_ms))
return {allowed, tokens}
"""
RELEASE_REFRESH_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
LINK_FRAME_RATE_LUA = """
local function charge(key, now, capacity, refill_per_ms, cost)
  local values = redis.call('HMGET', key, 'tokens', 'updated')
  local tokens = tonumber(values[1]) or capacity
  local updated = tonumber(values[2]) or now
  tokens = math.min(capacity, tokens + math.max(0, now - updated) * refill_per_ms)
  if tokens < cost then
    redis.call('HSET', key, 'tokens', tokens, 'updated', now)
    redis.call('PEXPIRE', key, math.ceil(capacity / refill_per_ms))
    return 0
  end
  redis.call('HSET', key, 'tokens', tokens - cost, 'updated', now)
  redis.call('PEXPIRE', key, math.ceil(capacity / refill_per_ms))
  return 1
end
local frames = charge(KEYS[1], tonumber(ARGV[1]), 120, 0.05, 1)
local bytes = charge(KEYS[2], tonumber(ARGV[1]), 8388608, 2097.152, tonumber(ARGV[2]))
return {frames, bytes}
"""


@dataclass(frozen=True, slots=True)
class FederationPrincipal:
    origin: str
    key_id: str
    silenced: bool = False
    source_ip: str | None = None


def federation_request_nonce(headers: Mapping[str, str]) -> str | None:
    version = headers.get("X-Kaede-Version")
    raw_nonce = headers.get("X-Kaede-Nonce")
    if version == "1":
        if raw_nonce is not None:
            raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_NONCE"})
        return None
    if version != "2":
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_UNSUPPORTED_VERSION"})
    if raw_nonce is None or REQUEST_NONCE_RE.fullmatch(raw_nonce) is None:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_NONCE"})
    return raw_nonce


def _deletion_control_inbox_request(request: Request) -> bool:
    """Allow a suspended peer to submit only removal-only inbox controls.

    Request authentication, replay protection, rate limits, event signatures,
    and semantic validation still run normally. This narrow transport bypass
    prevents an intermediary block from stranding already-disclosed media.
    """

    if request.url.path != "/_kaede/v1/inbox":
        return False
    payload = getattr(request.state, "federation_json", None)
    raw_events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(raw_events, list) or not raw_events:
        return False
    return all(
        isinstance(item, dict)
        and (
            item.get("type") == "media.delete"
            or terminal_room_event_ref(cast(dict[str, Any], item)) is not None
        )
        for item in raw_events
    )


def require_pinned_request_nonce(instance: Instance | None, nonce: str | None) -> None:
    if instance is not None and "request-nonce/1" in instance.capabilities and nonce is None:
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_NONCE_REQUIRED"})


def require_permission_schema(instance: Instance | None) -> None:
    """Reject imports whose peer did not negotiate Kaede's exact mask layout."""

    if instance is None or PERMISSION_SCHEMA_CAPABILITY not in instance.capabilities:
        raise HTTPException(
            status_code=426,
            detail={"code": "KAED_FED_PERMISSION_SCHEMA_REQUIRED"},
        )


async def consume_request_nonce(
    redis: Redis,
    settings: Settings,
    origin: str,
    nonce: str | None,
) -> None:
    if nonce is None:
        return
    accepted = await redis.set(
        f"federation:request-nonce:{origin}:{nonce}",
        "1",
        ex=settings.federation_clock_skew_seconds * 2 + 60,
        nx=True,
    )
    if not accepted:
        raise HTTPException(status_code=409, detail={"code": "KAED_FED_REPLAYED_REQUEST"})


async def lock_block_policy_shared(session: AsyncSession) -> None:
    """Fence federation work against exclusive block-policy administration."""

    await session.scalar(
        select(
            func.pg_advisory_xact_lock_shared(func.hashtextextended(BLOCK_POLICY_ADVISORY_NAME, 0))
        )
    )


def require_guild_federation_access(principal: FederationPrincipal) -> None:
    """Deny guild state pulls and writes from locally silenced peers."""

    if principal.silenced:
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_INSTANCE_SILENCED"},
        )


async def federation_event_policy_code(
    session: AsyncSession,
    origin: str,
    event_type: str,
    *,
    deletion_control: bool = False,
    event_context: object = None,
) -> str | None:
    """Recheck policy for one inbox event after prior events may commit."""

    # Authenticated, strictly validated removal-only controls must converge
    # even while ordinary federation with an instance is suspended. Holding a
    # multi-hop media/room deletion at an intermediary would strand bytes on
    # downstream replicas that the origin cannot address directly.
    if deletion_control:
        return None
    await lock_block_policy_shared(session)
    current_block = await matching_block(session, origin)
    if current_block is None:
        return None
    if current_block.level == "suspend":
        return "KAED_FED_INSTANCE_SUSPENDED"
    if federation_policy_holds_event(
        current_block.level,
        event_type,
        context=event_context,
    ):
        return "KAED_FED_INSTANCE_SILENCED"
    return None


def event_timestamp_allowed(
    event_timestamp_ms: int,
    *,
    now_ms: int,
    future_skew_seconds: int,
    retention_days: int,
    allow_past: bool = False,
) -> bool:
    """Bound durable-envelope replay without rejecting legitimate queue delay."""

    return (
        allow_past or now_ms - retention_days * 86_400_000 <= event_timestamp_ms
    ) and event_timestamp_ms <= now_ms + future_skew_seconds * 1000


async def self_instance(session: AsyncSession, settings: Settings) -> Instance:
    instance = await session.scalar(
        select(Instance).where(Instance.domain == settings.domain, Instance.is_self.is_(True))
    )
    if instance is None:
        raise RuntimeError("instance bootstrap is required")
    return instance


async def self_private_key(
    session: AsyncSession, settings: Settings
) -> tuple[str, Ed25519PrivateKey]:
    instance = await self_instance(session, settings)
    if (
        instance.current_key_id is None
        or instance.encrypted_private_key is None
        or instance.private_key_nonce is None
    ):
        raise RuntimeError("self instance has no signing key")
    raw = AESGCM(settings.secret_key_bytes).decrypt(
        instance.private_key_nonce,
        instance.encrypted_private_key,
        settings.domain.encode("ascii"),
    )
    return instance.current_key_id, Ed25519PrivateKey.from_private_bytes(raw)


async def matching_block(session: AsyncSession, domain: str) -> InstanceBlock | None:
    domain = normalize_domain(domain)
    labels = domain.split(".")
    candidates = [".".join(labels[index:]) for index in range(max(1, len(labels) - 1))]
    blocks = list(
        await session.scalars(select(InstanceBlock).where(InstanceBlock.domain.in_(candidates)))
    )
    applicable = [block for block in blocks if block.domain == domain or block.include_subdomains]
    if not applicable:
        return None
    suspended = [block for block in applicable if block.level == "suspend"]
    return max(suspended or applicable, key=lambda block: len(block.domain))


async def bounded_request_body(
    request: Request,
    *,
    max_bytes: int = MAX_FEDERATION_REQUEST_BYTES,
    too_large_code: str = "KAED_FED_BATCH_TOO_LARGE",
) -> bytes:
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            if not content_length.isascii() or not content_length.isdecimal():
                raise ValueError
            parsed_content_length = int(content_length)
            if not 0 <= parsed_content_length <= max_bytes:
                raise HTTPException(status_code=413, detail={"code": too_large_code})
        except ValueError:
            raise HTTPException(
                status_code=400, detail={"code": "KAED_FED_INVALID_CONTENT_LENGTH"}
            ) from None
    cached = getattr(request, "_body", None)
    if isinstance(cached, bytes):
        if len(cached) > max_bytes:
            raise HTTPException(status_code=413, detail={"code": too_large_code})
        return cached
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(status_code=413, detail={"code": too_large_code})
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body  # Starlette's own body cache.
    return body


async def enforce_origin_rate_limit(redis: Redis, origin: str) -> None:
    now_ms = int(time.time() * 1000)
    result = await cast(Any, redis.eval)(
        RATE_LIMIT_LUA,
        1,
        f"federation:rate:{origin}",
        str(now_ms),
        "100",
        "0.1",
        "1",
    )
    if not isinstance(result, (list, tuple)) or not result or int(result[0]) != 1:
        raise HTTPException(
            status_code=429,
            detail={"code": "KAED_RATE_LIMITED", "retry_after_ms": 1000},
            headers={"Retry-After": "1"},
        )


async def enforce_origin_event_rate_limit(redis: Redis, origin: str, cost: int) -> None:
    """Charge every event, valid or invalid, across HTTP and link transports."""

    if not 1 <= cost <= 100:
        raise ValueError("federation event rate cost is out of range")
    now_ms = int(time.time() * 1000)
    result = await cast(Any, redis.eval)(
        RATE_LIMIT_LUA,
        1,
        f"federation:event-rate:{origin}",
        str(now_ms),
        "200",
        "0.2",
        str(cost),
    )
    if not isinstance(result, (list, tuple)) or not result or int(result[0]) != 1:
        raise HTTPException(
            status_code=429,
            detail={"code": "KAED_RATE_LIMITED", "retry_after_ms": 1000},
            headers={"Retry-After": "1"},
        )


async def enforce_federation_link_frame_rate_limit(
    redis: Redis,
    origin: str,
    byte_length: int,
) -> None:
    """Bound authenticated hot-link frames and bytes before JSON parsing."""

    if not 0 <= byte_length <= MAX_FEDERATION_REQUEST_BYTES * 4:
        raise HTTPException(status_code=429, detail={"code": "KAED_RATE_LIMITED"})
    now_ms = int(time.time() * 1000)
    result = await cast(Any, redis.eval)(
        LINK_FRAME_RATE_LUA,
        2,
        f"federation:link-frame-rate:{origin}",
        f"federation:link-byte-rate:{origin}",
        str(now_ms),
        str(byte_length),
    )
    if (
        not isinstance(result, (list, tuple))
        or len(result) != 2
        or int(result[0]) != 1
        or int(result[1]) != 1
    ):
        raise HTTPException(
            status_code=429,
            detail={"code": "KAED_RATE_LIMITED", "retry_after_ms": 1_000},
            headers={"Retry-After": "1"},
        )


async def enforce_federation_route_rate_limit(
    redis: Redis,
    origin: str,
    route: str,
    *,
    capacity: int,
    refill_per_minute: int,
) -> None:
    """Apply an additional bounded bucket to an expensive public-guild route."""

    now_ms = int(time.time() * 1000)
    result = await cast(Any, redis.eval)(
        RATE_LIMIT_LUA,
        1,
        f"federation:route:{route}:{origin}",
        str(now_ms),
        str(capacity),
        str(refill_per_minute / 60_000),
        "1",
    )
    if not isinstance(result, (list, tuple)) or not result or int(result[0]) != 1:
        raise HTTPException(
            status_code=429,
            detail={"code": "KAED_RATE_LIMITED", "retry_after_ms": 60_000},
            headers={"Retry-After": "60"},
        )


async def enforce_federation_source_rate_limit(redis: Redis, source_ip: str) -> None:
    """Bound pre-auth work without letting spoofed origins spend a peer's bucket."""

    now_ms = int(time.time() * 1000)
    result = await cast(Any, redis.eval)(
        RATE_LIMIT_LUA,
        1,
        f"federation:preauth:{source_ip}",
        str(now_ms),
        "60",
        "0.06",
        "1",
    )
    if not isinstance(result, (list, tuple)) or not result or int(result[0]) != 1:
        raise HTTPException(
            status_code=429,
            detail={"code": "KAED_RATE_LIMITED", "retry_after_ms": 1000},
            headers={"Retry-After": "1"},
        )


def federation_client_ip(request: Request, settings: Settings) -> str:
    supplied_secret = request.headers.get("X-Kaede-Proxy-Secret")
    configured_secret = (
        settings.proxy_secret.get_secret_value() if settings.proxy_secret is not None else None
    )
    return resolve_client_ip(
        supplied_secret=supplied_secret,
        configured_secret=configured_secret,
        forwarded_for=request.headers.get("X-Forwarded-For"),
        direct_host=request.client.host if request.client is not None else None,
    )


def federation_websocket_client_ip(websocket: WebSocket, settings: Settings) -> str:
    supplied_secret = websocket.headers.get("X-Kaede-Proxy-Secret")
    configured_secret = (
        settings.proxy_secret.get_secret_value() if settings.proxy_secret is not None else None
    )
    return resolve_client_ip(
        supplied_secret=supplied_secret,
        configured_secret=configured_secret,
        forwarded_for=websocket.headers.get("X-Forwarded-For"),
        direct_host=websocket.client.host if websocket.client is not None else None,
    )


async def admit_unknown_key_refresh(redis: Redis, source_ip: str, origin: str) -> bool:
    """Consume the stricter unauthenticated key-discovery quotas."""

    now_ms = int(time.time() * 1000)
    for key, capacity, refill_per_ms in (
        (f"federation:key-refresh:source:{source_ip}", "5", "0.00008333333333333333"),
        (f"federation:key-refresh:origin:{origin}", "2", "0.00003333333333333333"),
    ):
        result = await cast(Any, redis.eval)(
            RATE_LIMIT_LUA,
            1,
            key,
            str(now_ms),
            capacity,
            refill_per_ms,
            "1",
        )
        if not isinstance(result, (list, tuple)) or not result or int(result[0]) != 1:
            raise HTTPException(
                status_code=429,
                detail={"code": "KAED_FED_KEY_REFRESH_RATE_LIMITED", "retry_after_ms": 30_000},
                headers={"Retry-After": "30"},
            )
    return True


async def refresh_event_signing_keys(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: FederationPrincipal,
    key_id: str,
) -> bool:
    """Refresh one unknown durable-event key without allowing discovery amplification.

    ``False`` means the sender should retry later: another worker may own the
    refresh, a recent network miss is cached, or the stricter discovery bucket
    is exhausted. A completed refresh returns ``True`` even when the requested
    key is absent so the caller can terminally reject that signed envelope.
    """

    if principal.source_ip is None:
        return False
    miss_key = f"federation:key-refresh:miss:{principal.origin}:{key_id}"
    if await redis.exists(miss_key):
        return False
    try:
        await admit_unknown_key_refresh(redis, principal.source_ip, principal.origin)
    except HTTPException:
        return False
    refresh_lock = f"federation:key-refresh:lock:{principal.origin}"
    refresh_owner = secrets.token_urlsafe(16)
    if not await redis.set(refresh_lock, refresh_owner, ex=30, nx=True):
        return False
    try:
        try:
            await ensure_peer(session, settings, principal.origin, force=True)
        except FederationNetworkError:
            await redis.set(miss_key, "1", ex=300)
            return False
    finally:
        await cast(Any, redis.eval)(
            RELEASE_REFRESH_LOCK_SCRIPT,
            1,
            refresh_lock,
            refresh_owner,
        )
    return True


async def validated_event_envelope(
    session: AsyncSession,
    settings: Settings,
    expected_origin: str,
    raw_envelope: object,
    *,
    allow_authority_attested_actor: bool = False,
) -> EventEnvelope:
    try:
        envelope = EventEnvelope.model_validate(raw_envelope)
    except ValueError as exc:
        raise ValueError("invalid signed event envelope") from exc
    serialized_envelope = envelope.model_dump(mode="json")
    authority_attested_actor = False
    if allow_authority_attested_actor and envelope.actor.domain != expected_origin:
        from app.bots.dm_capability import authority_attested_bot_dm_capability
        from app.bots.interaction_events import authority_attested_interaction_response
        from app.chat.expression_authorization import authority_attested_expression_use

        authority_attested_actor = bool(
            terminal_room_event_ref(serialized_envelope) is not None
            or authority_attested_group_event_ref(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=expected_origin,
                actor_id=envelope.actor.id,
                actor_domain=envelope.actor.domain,
            )
            is not None
            or guild_media_delete_request_ref(serialized_envelope) is not None
            or guild_authority_event_ref(
                envelope.type,
                envelope.context,
                expected_authority=expected_origin,
            )
            is not None
            or guild_message_authority_event_refs(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=expected_origin,
            )
            is not None
            or guild_crosspost_authority_event_ref(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=expected_origin,
            )
            is not None
            or authority_attested_bot_dm_capability(
                envelope.type,
                envelope.content,
                expected_authority=expected_origin,
                actor=(envelope.actor.id, envelope.actor.domain),
            )
            or authority_attested_interaction_response(
                envelope.type,
                envelope.content,
                expected_authority=expected_origin,
                actor=(envelope.actor.id, envelope.actor.domain),
            )
            or authority_attested_direct_poll_result(
                envelope.type,
                envelope.content,
                expected_authority=expected_origin,
                actor=(envelope.actor.id, envelope.actor.domain),
            )
            or authority_attested_dm_poll_mutation(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=expected_origin,
            )
            or authority_attested_dm_message_mutation(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=expected_origin,
                actor=(envelope.actor.id, envelope.actor.domain),
            )
            or authority_attested_forward_source(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=expected_origin,
                actor=(envelope.actor.id, envelope.actor.domain),
                event_timestamp_ms=envelope.ts,
            )
            or authority_attested_expression_use(
                envelope.type,
                envelope.content,
                envelope.context,
                expected_authority=expected_origin,
                actor=(envelope.actor.id, envelope.actor.domain),
            )
        )
    if envelope.origin != expected_origin or (
        envelope.actor.domain != expected_origin and not authority_attested_actor
    ):
        raise ValueError("signed event actor does not belong to its origin")
    if not event_timestamp_allowed(
        envelope.ts,
        now_ms=int(time.time() * 1000),
        future_skew_seconds=settings.federation_clock_skew_seconds,
        retention_days=settings.federation_event_retention_days,
    ):
        raise ValueError("signed event timestamp is outside the accepted window")
    signatures = envelope.signatures.get(expected_origin, {})
    if not signatures:
        raise ValueError("event envelope has no signature from its origin")
    if expected_origin == settings.domain:
        # In-process authority joins (for example A=B bot installs) must not
        # discover or HTTP-call the local instance as though it were a peer.
        # Verify the current self key directly; retained old-key envelopes can
        # still fall through to the ordinary cached-key path below.
        current_key_id, current_private_key = await self_private_key(session, settings)
        encoded = signatures.get(current_key_id)
        try:
            current_signature = (
                base64.b64decode(encoded, validate=True) if encoded is not None else b""
            )
        except (TypeError, ValueError):
            current_signature = b""
        if len(current_signature) == 64 and verify_envelope(
            serialized_envelope,
            current_signature,
            current_private_key.public_key(),
        ):
            return envelope

    async def verify_cached_signatures() -> tuple[bool, bool]:
        refresh_candidate = False
        for key_id, encoded in signatures.items():
            try:
                signature = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                continue
            if len(signature) != 64:
                continue
            peer_key = await session.get(PeerKey, (expected_origin, key_id))
            if peer_key is None or peer_key.expired_at is not None:
                refresh_candidate = True
                continue
            try:
                public_key = Ed25519PublicKey.from_public_bytes(peer_key.public_key)
            except (ValueError, TypeError):
                continue
            if verify_envelope(envelope.model_dump(mode="json"), signature, public_key):
                return True, refresh_candidate
        return False, refresh_candidate

    verified, refresh_candidate = await verify_cached_signatures()
    if verified:
        return envelope
    if refresh_candidate and expected_origin != settings.domain:
        # Direct proxy responses and guild gap-fill share this verifier. A peer
        # may rotate between our signed request and its signed response, so
        # force exactly one bounded trust-document refresh for an otherwise
        # well-formed signature made by an unknown/retired key. Structural,
        # origin, and timestamp checks above are never retried or bypassed.
        await ensure_peer(session, settings, expected_origin, force=True)
        verified, _unused_refresh_candidate = await verify_cached_signatures()
        if verified:
            return envelope
    raise ValueError("event envelope signature is invalid")


async def authenticate_federation(
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    redis: Redis = Depends(get_redis),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> FederationPrincipal:
    match = AUTHORIZATION_RE.fullmatch(request.headers.get("Authorization", ""))
    if match is None:
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_SIGNATURE_REQUIRED"})
    key_id = match.group("key")
    raw_signature = match.group("sig")
    if not KEY_ID_RE.fullmatch(key_id) or len(raw_signature) != 88:
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_BAD_SIGNATURE"})
    try:
        origin = normalize_domain(match.group("origin"))
        timestamp = int(request.headers.get("X-Kaede-Timestamp", ""))
        signature = base64.b64decode(raw_signature, validate=True)
    except (FederationNetworkError, ValueError):
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_BAD_SIGNATURE"}) from None
    if abs(int(time.time()) - timestamp) > settings.federation_clock_skew_seconds:
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_CLOCK_SKEW"})
    nonce = federation_request_nonce(request.headers)
    source_ip = federation_client_ip(request, settings)
    await enforce_federation_source_rate_limit(redis, source_ip)
    await lock_block_policy_shared(session)
    instance = await session.get(Instance, origin)
    if settings.federation_mode == "allowlist" and (
        instance is None or instance.federation_mode != "allowlist"
    ):
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_NOT_ALLOWLISTED"})
    if len(signature) != 64:
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_BAD_SIGNATURE"})
    # Complete and bound the request before unauthenticated key discovery can
    # consume outbound DNS/TLS resources.
    body = await bounded_request_body(request)
    peer_key = await session.get(PeerKey, (origin, key_id))
    if peer_key is None or peer_key_needs_refresh(peer_key, datetime.now(UTC)):
        miss_key = f"federation:key-refresh:miss:{origin}:{key_id}"
        if await redis.exists(miss_key):
            raise HTTPException(status_code=401, detail={"code": "KAED_FED_UNKNOWN_KEY"})
        if peer_key is None:
            admitted = await admit_unknown_key_refresh(redis, source_ip, origin)
            if not admitted:
                raise HTTPException(status_code=401, detail={"code": "KAED_FED_UNKNOWN_KEY"})
        # Serialize refreshes across API replicas. A second request fails closed
        # briefly instead of continuing to trust a stale or retired key while
        # the authoritative key set is being fetched.
        refresh_lock = f"federation:key-refresh:lock:{origin}"
        refresh_owner = secrets.token_urlsafe(16)
        if not await redis.set(refresh_lock, refresh_owner, ex=30, nx=True):
            raise HTTPException(status_code=401, detail={"code": "KAED_FED_UNKNOWN_KEY"})
        try:
            await ensure_peer(session, settings, origin, force=True)
        except FederationNetworkError:
            await redis.set(miss_key, "1", ex=300)
            raise HTTPException(status_code=401, detail={"code": "KAED_FED_UNKNOWN_KEY"}) from None
        finally:
            await cast(Any, redis.eval)(
                RELEASE_REFRESH_LOCK_SCRIPT,
                1,
                refresh_lock,
                refresh_owner,
            )
        peer_key = await session.get(PeerKey, (origin, key_id), populate_existing=True)
    if peer_key is None or peer_key.expired_at is not None:
        await redis.set(f"federation:key-refresh:miss:{origin}:{key_id}", "1", ex=300)
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_UNKNOWN_KEY"})
    instance = await session.get(Instance, origin, populate_existing=True)
    require_pinned_request_nonce(instance, nonce)
    signing_input = SigningInput(
        method=request.method,
        request_target=canonical_request_target(request.url.path, request.url.query),
        origin=origin,
        destination=settings.domain,
        timestamp=timestamp,
        content_hash=content_sha256(body),
        nonce=nonce,
    )
    public_key = Ed25519PublicKey.from_public_bytes(peer_key.public_key)
    if not verify_request(signing_input, signature, public_key):
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_BAD_SIGNATURE"})
    require_permission_schema(instance)
    # Charge the authenticated peer before allocating its per-request replay
    # key. This bounds nonce-key growth across a peer's source IPs, including
    # signed requests that are later rejected as malformed or replayed.
    await enforce_origin_rate_limit(redis, origin)
    await consume_request_nonce(redis, settings, origin, nonce)
    if body:
        try:
            if not hasattr(request.state, "federation_json"):
                request.state.federation_json = strict_json_loads(
                    body,
                    allow_floats=(
                        request.url.path.startswith("/_kaede/v1/channels/")
                        and request.url.path.endswith("/interactions")
                    ),
                )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={"code": "KAED_FED_INVALID_JSON"},
            ) from None
    try:
        hop = int(request.headers.get("X-Kaede-Hop", "0"))
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_HOP_LIMIT"}) from None
    if not 0 <= hop <= 5:
        raise HTTPException(status_code=508, detail={"code": "KAED_FED_HOP_LIMIT"})
    instance = await session.get(Instance, origin)
    if instance is not None:
        instance.last_seen_at = datetime.now(UTC)
    # Authentication may have discovered/rotated a key. Persist trust only after
    # that key successfully verifies this request; route work starts a fresh tx.
    await session.commit()
    # Fence the complete authenticated route on its fresh transaction. Policy
    # administration takes the exclusive counterpart, so a completed suspend
    # cannot race inbox, lookup, join, DM, or guild work after this recheck.
    await lock_block_policy_shared(session)
    current_block = await matching_block(session, origin)
    if (
        current_block is not None
        and current_block.level == "suspend"
        and not _deletion_control_inbox_request(request)
    ):
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_INSTANCE_SUSPENDED"})
    return FederationPrincipal(
        origin=origin,
        key_id=key_id,
        silenced=current_block is not None and current_block.level == "silence",
        source_ip=source_ip,
    )


async def authenticate_federation_websocket(
    websocket: WebSocket,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> FederationPrincipal:
    """Authenticate a signed, empty-body `GET /link` WebSocket upgrade."""

    match = AUTHORIZATION_RE.fullmatch(websocket.headers.get("Authorization", ""))
    if match is None:
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_SIGNATURE_REQUIRED"})
    key_id = match.group("key")
    raw_signature = match.group("sig")
    if not KEY_ID_RE.fullmatch(key_id) or len(raw_signature) != 88:
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_BAD_SIGNATURE"})
    try:
        origin = normalize_domain(match.group("origin"))
        timestamp = int(websocket.headers.get("X-Kaede-Timestamp", ""))
        signature = base64.b64decode(raw_signature, validate=True)
    except (FederationNetworkError, ValueError):
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_BAD_SIGNATURE"}) from None
    if abs(int(time.time()) - timestamp) > settings.federation_clock_skew_seconds:
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_CLOCK_SKEW"})
    nonce = federation_request_nonce(websocket.headers)
    source_ip = federation_websocket_client_ip(websocket, settings)
    await enforce_federation_source_rate_limit(redis, source_ip)
    await lock_block_policy_shared(session)
    instance = await session.get(Instance, origin)
    if settings.federation_mode == "allowlist" and (
        instance is None or instance.federation_mode != "allowlist"
    ):
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_NOT_ALLOWLISTED"})
    if len(signature) != 64:
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_BAD_SIGNATURE"})
    peer_key = await session.get(PeerKey, (origin, key_id))
    if peer_key is None or peer_key_needs_refresh(peer_key, datetime.now(UTC)):
        miss_key = f"federation:key-refresh:miss:{origin}:{key_id}"
        if await redis.exists(miss_key):
            raise HTTPException(status_code=401, detail={"code": "KAED_FED_UNKNOWN_KEY"})
        if peer_key is None:
            await admit_unknown_key_refresh(redis, source_ip, origin)
        refresh_lock = f"federation:key-refresh:lock:{origin}"
        refresh_owner = secrets.token_urlsafe(16)
        if not await redis.set(refresh_lock, refresh_owner, ex=30, nx=True):
            raise HTTPException(status_code=401, detail={"code": "KAED_FED_UNKNOWN_KEY"})
        try:
            try:
                await ensure_peer(session, settings, origin, force=True)
            except FederationNetworkError:
                await redis.set(miss_key, "1", ex=300)
                raise HTTPException(
                    status_code=401, detail={"code": "KAED_FED_UNKNOWN_KEY"}
                ) from None
        finally:
            await cast(Any, redis.eval)(
                RELEASE_REFRESH_LOCK_SCRIPT,
                1,
                refresh_lock,
                refresh_owner,
            )
        peer_key = await session.get(PeerKey, (origin, key_id), populate_existing=True)
    if peer_key is None or peer_key.expired_at is not None:
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_UNKNOWN_KEY"})
    instance = await session.get(Instance, origin, populate_existing=True)
    require_pinned_request_nonce(instance, nonce)
    signing_input = SigningInput(
        method="GET",
        request_target=canonical_request_target(websocket.url.path, websocket.url.query),
        origin=origin,
        destination=settings.domain,
        timestamp=timestamp,
        content_hash=content_sha256(b""),
        nonce=nonce,
    )
    public_key = Ed25519PublicKey.from_public_bytes(peer_key.public_key)
    if not verify_request(signing_input, signature, public_key):
        raise HTTPException(status_code=401, detail={"code": "KAED_FED_BAD_SIGNATURE"})
    require_permission_schema(instance)
    # Keep replay-key memory bounded by the authenticated-origin bucket even
    # when an attacker spreads signed upgrades over many source addresses.
    await enforce_origin_rate_limit(redis, origin)
    await consume_request_nonce(redis, settings, origin, nonce)
    try:
        hop = int(websocket.headers.get("X-Kaede-Hop", "0"))
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_HOP_LIMIT"}) from None
    if not 0 <= hop <= 5:
        raise HTTPException(status_code=508, detail={"code": "KAED_FED_HOP_LIMIT"})
    if instance is not None:
        instance.last_seen_at = datetime.now(UTC)
    await session.commit()
    await lock_block_policy_shared(session)
    current_block = await matching_block(session, origin)
    # A suspended link is restricted by the per-event policy check to exact
    # deletion controls; permitting the authenticated transport itself is
    # necessary for offline durable invalidation to converge.
    return FederationPrincipal(
        origin=origin,
        key_id=key_id,
        silenced=current_block is not None and current_block.level == "silence",
        source_ip=source_ip,
    )


def admin_authorized(request: Request, settings: Settings) -> None:
    expected = settings.admin_token.get_secret_value() if settings.admin_token else None
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if expected is None or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail={"code": "ADMIN_AUTHENTICATION_REQUIRED"})
