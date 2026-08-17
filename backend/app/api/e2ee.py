from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.chat.channel_access import (
    ChannelAccess,
    load_channel_access,
    lock_local_channel_mutation,
    publish_channel_dispatch,
)
from app.chat.e2ee import (
    E2EE_PROTOCOL_MLS_10,
    E2EE_SUITE_MLS_128,
    channel_encryption_policy_payload,
    validate_channel_encryption_policy,
    validate_channel_encryption_policy_transition,
    validate_e2ee_envelope,
)
from app.chat.e2ee_membership import (
    e2ee_policy_destinations,
    pause_local_e2ee_for_device_change,
    publish_e2ee_policy_updates,
    remote_e2ee_authorities_for_user,
)
from app.chat.events import publish_dispatch, user_topic
from app.chat.guild_revision import (
    federation_channel_state,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import channel_payload, message_payload
from app.chat.permissions import get_permissions, require_permissions
from app.chat.schemas import RequestModel
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.models import (
    Channel,
    DMConversation,
    E2EEDevice,
    E2EEKeyPackage,
    GuildMember,
    Message,
    MessageProjection,
    User,
)
from app.federation.client import signed_request
from app.federation.dm_storage import (
    admit_federated_dm_message,
    dm_message_storage_delta,
    lock_federated_dm_authority,
)
from app.federation.events import build_envelope, queue_event
from app.federation.network import FederationNetworkError, decode_federation_response_json
from app.federation.replication import profile_from_user
from app.voice.e2ee import MediaSessionRotationError, evict_channel_media_sessions

router = APIRouter(prefix="/api/v1/e2ee", tags=["end-to-end encryption"])
REGISTRATION_CHALLENGE_TTL_SECONDS = 300
KEY_PACKAGE_MAX_LIFETIME = timedelta(days=30)
MAX_ACTIVE_DEVICES = 25
MAX_AVAILABLE_KEY_PACKAGES_PER_DEVICE = 100
MAX_ROOM_E2EE_MEMBERS = 500
MAX_ROOM_E2EE_DEVICES = 48
DEVICE_CAPABILITIES = frozenset({"e2ee-mls/1", "e2ee-media/1"})


def encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_base64url(value: str, *, size: int | None = None, maximum: int | None = None) -> bytes:
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("value is not canonical URL-safe base64") from exc
    if encode_base64url(decoded) != value:
        raise ValueError("value is not canonical URL-safe base64")
    if size is not None and len(decoded) != size:
        raise ValueError(f"value must decode to exactly {size} bytes")
    if maximum is not None and len(decoded) > maximum:
        raise ValueError(f"value must decode to at most {maximum} bytes")
    return decoded


class DeviceChallengeRequest(RequestModel):
    identity_key: str = Field(min_length=43, max_length=43)
    credential_digest: str = Field(min_length=43, max_length=43)

    @field_validator("identity_key", "credential_digest")
    @classmethod
    def canonical_digest(cls, value: str) -> str:
        decode_base64url(value, size=32)
        return value


class DeviceRegister(RequestModel):
    challenge_id: str = Field(min_length=32, max_length=64)
    identity_key: str = Field(min_length=43, max_length=43)
    credential: str = Field(min_length=2, max_length=22_000)
    signature: str = Field(min_length=86, max_length=86)
    device_name: str = Field(min_length=1, max_length=100)
    platform: str
    capabilities: list[str] = Field(min_length=1, max_length=8)

    @field_validator("device_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("device name must not be blank")
        return cleaned

    @field_validator("platform")
    @classmethod
    def supported_platform(cls, value: str) -> str:
        if value not in {"web", "desktop", "android", "ios"}:
            raise ValueError("unsupported device platform")
        return value

    @field_validator("capabilities")
    @classmethod
    def supported_capabilities(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(item not in DEVICE_CAPABILITIES for item in value):
            raise ValueError("device capabilities are invalid")
        if "e2ee-mls/1" not in value:
            raise ValueError("device must support e2ee-mls/1")
        return sorted(value)


class KeyPackageUpload(RequestModel):
    cipher_suite: str
    expires_at: datetime
    packages: list[str] = Field(min_length=1, max_length=50)
    signature: str = Field(min_length=86, max_length=86)

    @field_validator("cipher_suite")
    @classmethod
    def supported_suite(cls, value: str) -> str:
        if value != E2EE_SUITE_MLS_128:
            raise ValueError("unsupported MLS cipher suite")
        return value

    @field_validator("packages")
    @classmethod
    def bounded_packages(cls, value: list[str]) -> list[str]:
        for item in value:
            decode_base64url(item, maximum=32_768)
        if len(set(value)) != len(value):
            raise ValueError("key packages must be unique")
        return value

    @model_validator(mode="after")
    def valid_expiry(self) -> KeyPackageUpload:
        if self.expires_at.tzinfo is None:
            raise ValueError("key package expiry requires a timezone")
        return self


class RoomProposalRequest(RequestModel):
    sender_device_id: str = Field(pattern=r"^ked_[A-Za-z0-9_-]{43}$")


class RoomActivationRequest(RoomProposalRequest):
    policy_generation: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    epoch: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    commit: str = Field(min_length=2, max_length=87_384)
    welcome: str = Field(min_length=2, max_length=87_384)

    @field_validator("commit", "welcome")
    @classmethod
    def bounded_mls_message(cls, value: str) -> str:
        decode_base64url(value, maximum=64 * 1024)
        return value


class RoomRekeyActivationRequest(RoomActivationRequest):
    proposal_id: str = Field(pattern=r"^[A-Za-z0-9_-]{32,64}$")


def registration_signing_input(
    challenge: bytes,
    user: User,
    session_id: str,
    identity_key: bytes,
    credential_digest: bytes,
) -> bytes:
    return b"\x00".join(
        (
            b"kaede-device-registration-v1",
            challenge,
            f"{user.id}@{user.origin_domain}".encode(),
            session_id.encode(),
            identity_key,
            credential_digest,
        )
    )


def key_package_signing_input(
    device_id: str,
    cipher_suite: str,
    expires_at: datetime,
    package_digests: list[bytes],
) -> bytes:
    return b"\x00".join(
        (
            b"kaede-key-package-upload-v1",
            device_id.encode(),
            cipher_suite.encode(),
            # RFC 3339 milliseconds are the protocol representation used by
            # both JavaScript (Date.toISOString) and the native clients.  Do
            # not let Python's adaptive microsecond rendering make an
            # otherwise valid client signature unverifiable.
            expires_at.astimezone(UTC).isoformat(timespec="milliseconds").encode(),
            *package_digests,
        )
    )


def render_device(device: E2EEDevice, *, own: bool) -> dict[str, object]:
    return {
        "id": device.id,
        "user_id": str(device.user_id),
        "user_domain": device.user_domain,
        "identity_key": encode_base64url(device.identity_key),
        "credential": encode_base64url(device.credential),
        "device_name": device.device_name,
        "platform": device.platform,
        "capabilities": device.capabilities,
        "trust_state": device.trust_state if own else "unverified",
        "device_generation": str(device.device_generation),
        "last_seen_at": device.last_seen_at.isoformat(),
        "created_at": device.created_at.isoformat(),
        "revoked_at": device.revoked_at.isoformat() if device.revoked_at is not None else None,
    }


async def queue_device_change_updates(
    session: AsyncSession,
    settings: Settings,
    user: User,
    paused_channels: list[Channel],
) -> set[str]:
    destinations = await remote_e2ee_authorities_for_user(session, settings, user)
    if destinations:
        envelope = await build_envelope(
            session,
            settings,
            "e2ee.device-list.changed",
            user,
            {"profile": profile_from_user(user)},
        )
        for destination in destinations:
            await queue_event(session, settings, destination, envelope)
    for channel in paused_channels:
        channel_destinations = await e2ee_policy_destinations(session, settings, channel)
        if not channel_destinations:
            continue
        envelope = await build_envelope(
            session,
            settings,
            "e2ee.room-policy.changed",
            user,
            {
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "encryption_policy": channel_encryption_policy_payload(channel),
            },
        )
        for destination in channel_destinations:
            await queue_event(session, settings, destination, envelope)
        destinations.update(channel_destinations)
    return destinations


@router.post("/devices/challenge", status_code=201)
async def create_device_challenge(
    payload: DeviceChallengeRequest,
    auth: AuthenticatedUser = Depends(require_user),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str | int]:
    identity_key = decode_base64url(payload.identity_key, size=32)
    credential_digest = decode_base64url(payload.credential_digest, size=32)
    challenge_id = secrets.token_urlsafe(24)
    challenge = secrets.token_bytes(32)
    signing_input = registration_signing_input(
        challenge,
        auth.user,
        auth.grant.session_id,
        identity_key,
        credential_digest,
    )
    await redis.setex(
        f"e2ee:device-challenge:{challenge_id}",
        REGISTRATION_CHALLENGE_TTL_SECONDS,
        json.dumps(
            {
                "user_id": str(auth.user.id),
                "user_domain": auth.user.origin_domain,
                "session_id": auth.grant.session_id,
                "identity_key": payload.identity_key,
                "credential_digest": payload.credential_digest,
                "signing_input": encode_base64url(signing_input),
            },
            separators=(",", ":"),
        ),
    )
    return {
        "challenge_id": challenge_id,
        "signing_input": encode_base64url(signing_input),
        "expires_in": REGISTRATION_CHALLENGE_TTL_SECONDS,
        "domain": settings.domain,
    }


@router.post("/devices", status_code=201)
async def register_device(
    payload: DeviceRegister,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    challenge_key = f"e2ee:device-challenge:{payload.challenge_id}"
    raw_challenge = await redis.getdel(challenge_key)
    if not isinstance(raw_challenge, str):
        raise HTTPException(status_code=409, detail={"code": "E2EE_DEVICE_CHALLENGE_EXPIRED"})
    try:
        challenge = json.loads(raw_challenge)
    except json.JSONDecodeError as exc:
        raise RuntimeError("stored E2EE device challenge is corrupt") from exc
    identity_key = decode_base64url(payload.identity_key, size=32)
    credential = decode_base64url(payload.credential, maximum=16_384)
    credential_digest = hashlib.sha256(credential).digest()
    if challenge != {
        "user_id": str(auth.user.id),
        "user_domain": auth.user.origin_domain,
        "session_id": auth.grant.session_id,
        "identity_key": payload.identity_key,
        "credential_digest": encode_base64url(credential_digest),
        "signing_input": challenge.get("signing_input"),
    }:
        raise HTTPException(status_code=409, detail={"code": "E2EE_DEVICE_CHALLENGE_MISMATCH"})
    signing_input = decode_base64url(str(challenge["signing_input"]), maximum=1024)
    signature = decode_base64url(payload.signature, size=64)
    try:
        Ed25519PublicKey.from_public_bytes(identity_key).verify(signature, signing_input)
    except (InvalidSignature, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"code": "E2EE_DEVICE_PROOF_INVALID"}) from exc

    locked_user = await session.scalar(
        select(User)
        .where(
            User.id == auth.user.id,
            User.origin_domain == auth.user.origin_domain,
        )
        .with_for_update()
    )
    if locked_user is None:
        raise RuntimeError("authenticated user disappeared")
    existing = await session.scalar(
        select(E2EEDevice).where(
            E2EEDevice.user_id == locked_user.id,
            E2EEDevice.user_domain == locked_user.origin_domain,
            E2EEDevice.identity_key == identity_key,
        )
    )
    if existing is not None:
        if existing.revoked_at is not None:
            raise HTTPException(status_code=409, detail={"code": "E2EE_DEVICE_REVOKED"})
        if existing.credential != credential or existing.platform != payload.platform:
            raise HTTPException(status_code=409, detail={"code": "E2EE_DEVICE_IDENTITY_CONFLICT"})
        existing.last_seen_at = datetime.now(UTC)
        existing.device_name = payload.device_name
        existing.capabilities = payload.capabilities
        await session.commit()
        return render_device(existing, own=True)
    active_count = len(
        list(
            await session.scalars(
                select(E2EEDevice.id).where(
                    E2EEDevice.user_id == locked_user.id,
                    E2EEDevice.user_domain == locked_user.origin_domain,
                    E2EEDevice.revoked_at.is_(None),
                )
            )
        )
    )
    if active_count >= MAX_ACTIVE_DEVICES:
        raise HTTPException(status_code=409, detail={"code": "E2EE_DEVICE_LIMIT_REACHED"})
    locked_user.e2ee_device_generation += 1
    device_id = "ked_" + encode_base64url(
        hashlib.sha256(
            f"{locked_user.id}@{locked_user.origin_domain}\0".encode() + identity_key
        ).digest()
    )
    device = E2EEDevice(
        id=device_id,
        user_id=locked_user.id,
        user_domain=locked_user.origin_domain,
        identity_key=identity_key,
        credential=credential,
        device_name=payload.device_name,
        platform=payload.platform,
        capabilities=payload.capabilities,
        registered_session_id=auth.grant.session_id,
        device_generation=locked_user.e2ee_device_generation,
    )
    session.add(device)
    await session.flush()
    paused_channels = await pause_local_e2ee_for_device_change(session, settings, locked_user)
    delivery_wakes = await queue_device_change_updates(
        session, settings, locked_user, paused_channels
    )
    await session.commit()
    await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    if delivery_wakes:
        from app.tasks import federation_deliver

        for destination in delivery_wakes:
            await enqueue_best_effort(federation_deliver, destination)
    await publish_dispatch(
        redis,
        user_topic(settings.domain, locked_user.id),
        "E2EE_DEVICE_LIST_UPDATE",
        {
            "generation": str(locked_user.e2ee_device_generation),
            "device": render_device(device, own=True),
        },
    )
    return render_device(device, own=True)


@router.get("/devices")
async def list_devices(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    devices = list(
        await session.scalars(
            select(E2EEDevice)
            .where(
                E2EEDevice.user_id == auth.user.id,
                E2EEDevice.user_domain == auth.user.origin_domain,
            )
            .order_by(E2EEDevice.device_generation)
        )
    )
    now = datetime.now(UTC)
    available_counts: dict[str, int] = {
        str(device_id): int(count)
        for device_id, count in (
            await session.execute(
                select(E2EEKeyPackage.device_id, func.count(E2EEKeyPackage.id))
                .where(
                    E2EEKeyPackage.claimed_at.is_(None),
                    E2EEKeyPackage.expires_at > now,
                )
                .group_by(E2EEKeyPackage.device_id)
            )
        ).all()
    }
    rendered = []
    for device in devices:
        item = render_device(device, own=True)
        item["available_key_packages"] = int(available_counts.get(device.id, 0))
        rendered.append(item)
    return {
        "generation": str(auth.user.e2ee_device_generation),
        "devices": rendered,
    }


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(
    device_id: str,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    locked_user = await session.scalar(
        select(User)
        .where(User.id == auth.user.id, User.origin_domain == auth.user.origin_domain)
        .with_for_update()
    )
    device = await session.scalar(
        select(E2EEDevice)
        .where(
            E2EEDevice.id == device_id,
            E2EEDevice.user_id == auth.user.id,
            E2EEDevice.user_domain == auth.user.origin_domain,
        )
        .with_for_update()
    )
    if locked_user is None or device is None:
        raise HTTPException(status_code=404, detail={"code": "E2EE_DEVICE_NOT_FOUND"})
    paused_channels: list[Channel] = []
    delivery_wakes: set[str] = set()
    if device.revoked_at is None:
        device.revoked_at = datetime.now(UTC)
        locked_user.e2ee_device_generation += 1
        await session.execute(
            delete(E2EEKeyPackage).where(
                E2EEKeyPackage.device_id == device.id,
                E2EEKeyPackage.claimed_at.is_(None),
            )
        )
        paused_channels = await pause_local_e2ee_for_device_change(session, settings, locked_user)
        delivery_wakes = await queue_device_change_updates(
            session, settings, locked_user, paused_channels
        )
    await session.commit()
    await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    if delivery_wakes:
        from app.tasks import federation_deliver

        for destination in delivery_wakes:
            await enqueue_best_effort(federation_deliver, destination)
    await publish_dispatch(
        redis,
        user_topic(settings.domain, locked_user.id),
        "E2EE_DEVICE_LIST_UPDATE",
        {
            "generation": str(locked_user.e2ee_device_generation),
            "revoked_device_id": device.id,
        },
    )
    return Response(status_code=204)


@router.post("/devices/{device_id}/key-packages", status_code=201)
async def upload_key_packages(
    device_id: str,
    payload: KeyPackageUpload,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    now = datetime.now(UTC)
    expiry = payload.expires_at.astimezone(UTC)
    if expiry <= now or expiry > now + KEY_PACKAGE_MAX_LIFETIME:
        raise HTTPException(status_code=400, detail={"code": "E2EE_KEY_PACKAGE_EXPIRY_INVALID"})
    device = await session.scalar(
        select(E2EEDevice)
        .where(
            E2EEDevice.id == device_id,
            E2EEDevice.user_id == auth.user.id,
            E2EEDevice.user_domain == auth.user.origin_domain,
            E2EEDevice.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if device is None:
        raise HTTPException(status_code=404, detail={"code": "E2EE_DEVICE_NOT_FOUND"})
    package_bytes = [decode_base64url(item, maximum=32_768) for item in payload.packages]
    package_digests = [hashlib.sha256(item).digest() for item in package_bytes]
    signing_input = key_package_signing_input(
        device.id,
        payload.cipher_suite,
        expiry,
        package_digests,
    )
    try:
        Ed25519PublicKey.from_public_bytes(device.identity_key).verify(
            decode_base64url(payload.signature, size=64),
            signing_input,
        )
    except (InvalidSignature, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"code": "E2EE_DEVICE_PROOF_INVALID"}) from exc
    available = len(
        list(
            await session.scalars(
                select(E2EEKeyPackage.id).where(
                    E2EEKeyPackage.device_id == device.id,
                    E2EEKeyPackage.claimed_at.is_(None),
                    E2EEKeyPackage.expires_at > now,
                )
            )
        )
    )
    if available + len(package_bytes) > MAX_AVAILABLE_KEY_PACKAGES_PER_DEVICE:
        raise HTTPException(status_code=409, detail={"code": "E2EE_KEY_PACKAGE_LIMIT_REACHED"})
    inserted_ids: list[str] = []
    for package, digest in zip(package_bytes, package_digests, strict=True):
        package_id = encode_base64url(digest)
        existing = await session.get(E2EEKeyPackage, package_id)
        if existing is not None:
            if existing.device_id != device.id or existing.package_data != package:
                raise HTTPException(status_code=409, detail={"code": "E2EE_KEY_PACKAGE_CONFLICT"})
            continue
        session.add(
            E2EEKeyPackage(
                id=package_id,
                device_id=device.id,
                user_id=device.user_id,
                user_domain=device.user_domain,
                cipher_suite=payload.cipher_suite,
                package_data=package,
                expires_at=expiry,
            )
        )
        inserted_ids.append(package_id)
    device.last_seen_at = now
    await session.commit()
    return {"inserted": inserted_ids, "available": available + len(inserted_ids)}


async def require_room_policy_authority(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    *,
    allow_remote_authority: bool = False,
) -> DMConversation | None:
    if access.guild is not None:
        if access.guild.origin_domain != settings.domain and not allow_remote_authority:
            raise HTTPException(status_code=409, detail={"code": "E2EE_AUTHORITY_REMOTE"})
        await require_permissions(
            session,
            redis,
            access.guild,
            actor,
            Permission.MANAGE_CHANNELS,
            channel=access.channel,
        )
        return None
    conversation = await session.get(
        DMConversation,
        (access.channel.id, access.channel.origin_domain),
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if conversation.authority_domain != settings.domain and not allow_remote_authority:
        raise HTTPException(status_code=409, detail={"code": "E2EE_AUTHORITY_REMOTE"})
    if conversation.type == "group" and (
        conversation.owner_id,
        conversation.owner_domain,
    ) != (actor.id, actor.origin_domain):
        raise HTTPException(status_code=403, detail={"code": "GROUP_DM_OWNER_REQUIRED"})
    return conversation


def room_authority_domain(
    settings: Settings,
    access: ChannelAccess,
    conversation: DMConversation | None,
) -> str:
    if access.guild is not None:
        return access.guild.origin_domain
    if conversation is None:
        return settings.domain
    return conversation.authority_domain


async def proxy_room_e2ee_request(
    session: AsyncSession,
    settings: Settings,
    authority: str,
    path: str,
    *,
    channel: Channel,
    actor: User,
    body: dict[str, object],
) -> dict[str, object]:
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            authority,
            path,
            payload={
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "actor": profile_from_user(actor),
                **body,
            },
            request_timeout=15,
            max_response_bytes=8 * 1024 * 1024,
        )
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "E2EE_ROOM_AUTHORITY_UNREACHABLE"},
        ) from exc
    try:
        decoded = decode_federation_response_json(response)
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "E2EE_ROOM_AUTHORITY_INVALID_RESPONSE"},
        ) from exc
    if response.status_code != 200:
        detail = decoded if isinstance(decoded, dict) else {}
        upstream = detail.get("detail") if isinstance(detail.get("detail"), dict) else detail
        raise HTTPException(
            status_code=response.status_code,
            detail=upstream or {"code": "E2EE_ROOM_AUTHORITY_REJECTED"},
        )
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=502,
            detail={"code": "E2EE_ROOM_AUTHORITY_INVALID_RESPONSE"},
        )
    return decoded


async def apply_remote_active_policy(
    session: AsyncSession,
    channel: Channel,
    rendered: dict[str, object],
) -> None:
    incoming = validate_channel_encryption_policy(
        {
            "mode": rendered.get("encryption_mode"),
            "state": rendered.get("encryption_state"),
            "generation": rendered.get("encryption_policy_generation"),
            "protocol": rendered.get("encryption_protocol"),
            "suite": rendered.get("encryption_suite"),
            "group_id": rendered.get("encryption_group_id"),
            "epoch": rendered.get("encryption_epoch"),
        }
    )
    validate_channel_encryption_policy_transition(channel, incoming, label="proxied room")
    channel.encryption_mode = str(incoming["mode"])
    channel.encryption_state = str(incoming["state"])
    channel.encryption_policy_generation = int(incoming["generation"])
    channel.encryption_protocol = str(incoming["protocol"])
    channel.encryption_suite = str(incoming["suite"])
    channel.encryption_group_id = str(incoming["group_id"])
    channel.encryption_epoch = int(incoming["epoch"])
    activated_at = rendered.get("encryption_activated_at")
    channel.encryption_activated_at = (
        datetime.fromisoformat(activated_at) if isinstance(activated_at, str) else datetime.now(UTC)
    )
    await session.commit()


async def room_participants(
    session: AsyncSession,
    redis: Redis,
    access: ChannelAccess,
) -> list[User]:
    if access.guild is None:
        return access.participants
    members = list(
        await session.scalars(
            select(User)
            .join(
                GuildMember,
                (GuildMember.user_id == User.id) & (GuildMember.user_domain == User.origin_domain),
            )
            .where(
                GuildMember.guild_id == access.guild.id,
                GuildMember.guild_domain == access.guild.origin_domain,
            )
            .order_by(User.origin_domain, User.id)
            .limit(MAX_ROOM_E2EE_MEMBERS + 1)
        )
    )
    if len(members) > MAX_ROOM_E2EE_MEMBERS:
        raise HTTPException(status_code=409, detail={"code": "E2EE_ROOM_MEMBER_LIMIT"})
    visible: list[User] = []
    for member in members:
        permissions = await get_permissions(
            session,
            redis,
            access.guild,
            member,
            channel=access.channel,
        )
        if permissions & Permission.VIEW_CHANNEL:
            visible.append(member)
    return visible


async def require_active_sender_device(
    session: AsyncSession,
    actor: User,
    device_id: str,
) -> E2EEDevice:
    device = await session.scalar(
        select(E2EEDevice).where(
            E2EEDevice.id == device_id,
            E2EEDevice.user_id == actor.id,
            E2EEDevice.user_domain == actor.origin_domain,
            E2EEDevice.revoked_at.is_(None),
        )
    )
    if device is None:
        raise HTTPException(status_code=404, detail={"code": "E2EE_DEVICE_NOT_FOUND"})
    return device


async def claim_local_room_key_packages(
    session: AsyncSession,
    participants: list[User],
    *,
    claimant_ref: tuple[int, str],
    excluded_device_id: str,
    max_devices: int = MAX_ROOM_E2EE_DEVICES,
) -> list[dict[str, str]]:
    now = datetime.now(UTC)
    claimed: list[dict[str, str]] = []
    for participant in participants:
        if not participant.is_local:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "E2EE_REMOTE_DEVICE_DISCOVERY_REQUIRED",
                    "user_id": str(participant.id),
                    "user_domain": participant.origin_domain,
                },
            )
        devices = list(
            await session.scalars(
                select(E2EEDevice)
                .where(
                    E2EEDevice.user_id == participant.id,
                    E2EEDevice.user_domain == participant.origin_domain,
                    E2EEDevice.revoked_at.is_(None),
                )
                .order_by(E2EEDevice.device_generation)
            )
        )
        if not devices:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "E2EE_PARTICIPANT_DEVICE_MISSING",
                    "user_id": str(participant.id),
                    "user_domain": participant.origin_domain,
                },
            )
        for device in devices:
            if device.id == excluded_device_id:
                continue
            if len(claimed) >= max_devices:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "E2EE_ROOM_DEVICE_LIMIT"},
                )
            package = await session.scalar(
                select(E2EEKeyPackage)
                .where(
                    E2EEKeyPackage.device_id == device.id,
                    E2EEKeyPackage.claimed_at.is_(None),
                    E2EEKeyPackage.expires_at > now,
                    E2EEKeyPackage.cipher_suite == E2EE_SUITE_MLS_128,
                )
                .order_by(E2EEKeyPackage.expires_at, E2EEKeyPackage.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if package is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "E2EE_KEY_PACKAGE_UNAVAILABLE",
                        "device_id": device.id,
                    },
                )
            package.claimed_at = now
            package.claimed_by_id = claimant_ref[0]
            package.claimed_by_domain = claimant_ref[1]
            claimed.append(
                {
                    "user_id": str(participant.id),
                    "user_domain": participant.origin_domain,
                    "device_id": device.id,
                    "identity_key": encode_base64url(device.identity_key),
                    "credential": encode_base64url(device.credential),
                    "key_package": encode_base64url(package.package_data),
                }
            )
    return claimed


def validate_claimed_package(
    raw: object,
    *,
    expected_user: tuple[int, str],
) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {
        "user_id",
        "user_domain",
        "device_id",
        "identity_key",
        "credential",
        "key_package",
    }:
        raise ValueError("remote key package response is invalid")
    if (raw.get("user_id"), raw.get("user_domain")) != (
        str(expected_user[0]),
        expected_user[1],
    ):
        raise ValueError("remote key package belongs to the wrong user")
    device_id = raw.get("device_id")
    if not isinstance(device_id, str) or not device_id.startswith("ked_") or len(device_id) != 47:
        raise ValueError("remote key package device ID is invalid")
    identity_key = raw.get("identity_key")
    credential = raw.get("credential")
    key_package = raw.get("key_package")
    if not all(isinstance(item, str) for item in (identity_key, credential, key_package)):
        raise ValueError("remote key package encoding is invalid")
    decode_base64url(str(identity_key), size=32)
    decode_base64url(str(credential), maximum=16_384)
    decode_base64url(str(key_package), maximum=32_768)
    return {key: str(value) for key, value in raw.items()}


async def claim_room_key_packages(
    session: AsyncSession,
    settings: Settings,
    participants: list[User],
    *,
    claimant: User,
    channel: Channel,
    excluded_device_id: str,
) -> list[dict[str, str]]:
    local = [
        participant for participant in participants if participant.origin_domain == settings.domain
    ]
    claimed = await claim_local_room_key_packages(
        session,
        local,
        claimant_ref=(claimant.id, claimant.origin_domain),
        excluded_device_id=excluded_device_id,
        max_devices=MAX_ROOM_E2EE_DEVICES,
    )
    for participant in participants:
        if participant.origin_domain == settings.domain:
            continue
        if len(claimed) >= MAX_ROOM_E2EE_DEVICES:
            raise HTTPException(status_code=409, detail={"code": "E2EE_ROOM_DEVICE_LIMIT"})
        try:
            response = await signed_request(
                session,
                settings,
                "POST",
                participant.origin_domain,
                "/_kaede/v1/e2ee/key-packages/claim",
                payload={
                    "channel_id": str(channel.id),
                    "channel_domain": channel.origin_domain,
                    "claimant_id": str(claimant.id),
                    "claimant_domain": claimant.origin_domain,
                    "target_id": str(participant.id),
                    "target_domain": participant.origin_domain,
                    "excluded_device_id": (
                        excluded_device_id
                        if participant.origin_domain == claimant.origin_domain
                        else None
                    ),
                    "max_devices": MAX_ROOM_E2EE_DEVICES - len(claimed),
                },
                request_timeout=8,
                max_response_bytes=1024 * 1024,
            )
        except FederationNetworkError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "E2EE_PARTICIPANT_HOME_UNREACHABLE"},
            ) from exc
        raw = decode_federation_response_json(response)
        if response.status_code != 200:
            raw_detail = raw.get("detail") if isinstance(raw, dict) else None
            upstream_code = raw_detail.get("code") if isinstance(raw_detail, dict) else None
            code = (
                "E2EE_ROOM_DEVICE_LIMIT"
                if upstream_code == "E2EE_ROOM_DEVICE_LIMIT"
                else "E2EE_KEY_PACKAGE_UNAVAILABLE"
                if response.status_code == 409
                else "E2EE_PARTICIPANT_HOME_REJECTED"
            )
            raise HTTPException(
                status_code=409 if response.status_code == 409 else 502,
                detail={"code": code},
            )
        if not isinstance(raw, dict) or not isinstance(raw.get("key_packages"), list):
            raise HTTPException(status_code=502, detail={"code": "E2EE_PARTICIPANT_HOME_REJECTED"})
        if len(raw["key_packages"]) > MAX_ROOM_E2EE_DEVICES - len(claimed):
            raise HTTPException(status_code=502, detail={"code": "E2EE_PARTICIPANT_HOME_REJECTED"})
        try:
            remote = [
                validate_claimed_package(
                    item,
                    expected_user=(participant.id, participant.origin_domain),
                )
                for item in raw["key_packages"]
            ]
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "E2EE_PARTICIPANT_HOME_REJECTED"},
            ) from exc
        if not remote:
            raise HTTPException(status_code=409, detail={"code": "E2EE_PARTICIPANT_DEVICE_MISSING"})
        claimed.extend(remote)
    return claimed


async def publish_policy_update(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
) -> None:
    if access.guild is not None:
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            actor,
            "guild.channel.update",
            {"channel": federation_channel_state(access.channel)},
            channel=access.channel,
        )


@router.post("/channels/{channel_id}/propose")
async def propose_room_encryption(
    channel_id: EntityRef,
    payload: RoomProposalRequest,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not settings.e2ee_activation_enabled:
        raise HTTPException(status_code=403, detail={"code": "E2EE_ACTIVATION_DISABLED"})
    access = await load_channel_access(session, settings, auth.user, channel_id)
    conversation = await require_room_policy_authority(
        session,
        redis,
        settings,
        access,
        auth.user,
        allow_remote_authority=True,
    )
    authority = room_authority_domain(settings, access, conversation)
    if authority != settings.domain:
        await require_active_sender_device(session, auth.user, payload.sender_device_id)
        return await proxy_room_e2ee_request(
            session,
            settings,
            authority,
            "/_kaede/v1/e2ee/rooms/propose",
            channel=access.channel,
            actor=auth.user,
            body={"sender_device_id": payload.sender_device_id},
        )
    access = await lock_local_channel_mutation(session, settings, access)
    await require_room_policy_authority(session, redis, settings, access, auth.user)
    if auth.user.origin_domain == settings.domain:
        await require_active_sender_device(session, auth.user, payload.sender_device_id)
    channel = access.channel
    if channel.type not in {0, 1, 2, 5}:
        raise HTTPException(status_code=400, detail={"code": "NOT_TEXT_CHANNEL"})
    if channel.encryption_mode != "plaintext" or channel.encryption_state not in {
        "plaintext",
        "failed",
        "proposed",
    }:
        raise HTTPException(status_code=409, detail={"code": "E2EE_POLICY_ALREADY_EXISTS"})
    # A proposal is intentionally replaceable. The response can be lost after its
    # one-use key packages have been claimed, and the browser may disappear before
    # it persists the newly-created MLS group. Incrementing the generation makes
    # every earlier commit stale while allowing the user to retry without an
    # operator repairing the channel row.
    participants = await room_participants(session, redis, access)
    packages = await claim_room_key_packages(
        session,
        settings,
        participants,
        claimant=auth.user,
        channel=channel,
        excluded_device_id=payload.sender_device_id,
    )
    if not packages:
        raise HTTPException(status_code=409, detail={"code": "E2EE_NO_OTHER_DEVICES"})
    channel.encryption_policy_generation += 1
    channel.encryption_state = "proposed"
    channel.encryption_protocol = E2EE_PROTOCOL_MLS_10
    channel.encryption_suite = E2EE_SUITE_MLS_128
    channel.encryption_group_id = encode_base64url(secrets.token_bytes(32))
    channel.encryption_epoch = None
    await publish_policy_update(session, redis, settings, access, auth.user)
    await session.commit()
    if access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    rendered = channel_payload(channel)
    await publish_channel_dispatch(redis, access, "CHANNEL_UPDATE", rendered)
    return {
        "policy": channel_encryption_policy_payload(channel),
        "key_packages": packages,
    }


@router.post("/channels/{channel_id}/activate")
async def activate_room_encryption(
    channel_id: EntityRef,
    payload: RoomActivationRequest,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    remote_conversation = await require_room_policy_authority(
        session,
        redis,
        settings,
        access,
        auth.user,
        allow_remote_authority=True,
    )
    authority = room_authority_domain(settings, access, remote_conversation)
    if authority != settings.domain:
        await require_active_sender_device(session, auth.user, payload.sender_device_id)
        rendered = await proxy_room_e2ee_request(
            session,
            settings,
            authority,
            "/_kaede/v1/e2ee/rooms/activate",
            channel=access.channel,
            actor=auth.user,
            body=payload.model_dump(),
        )
        await apply_remote_active_policy(session, access.channel, rendered)
        return rendered
    access = await lock_local_channel_mutation(session, settings, access)
    conversation = await require_room_policy_authority(session, redis, settings, access, auth.user)
    if auth.user.origin_domain == settings.domain:
        await require_active_sender_device(session, auth.user, payload.sender_device_id)
    channel = access.channel
    initial_activation = (
        channel.encryption_state == "proposed"
        and channel.encryption_mode == "plaintext"
        and channel.encryption_policy_generation == int(payload.policy_generation)
        and int(payload.epoch) == 1
        and channel.encryption_group_id is not None
    )
    authorized_rekey = (
        channel.encryption_state == "activating"
        and channel.encryption_mode == "e2ee"
        and channel.encryption_policy_generation == int(payload.policy_generation)
        and channel.encryption_epoch == 1
        and channel.encryption_group_id is not None
    )
    if not initial_activation and not authorized_rekey:
        raise HTTPException(status_code=409, detail={"code": "E2EE_POLICY_CONTEXT_MISMATCH"})
    # Decode both messages before changing policy. The server cannot inspect MLS
    # semantics, but malformed/unbounded transport never reaches durable state.
    decode_base64url(payload.commit, maximum=64 * 1024)
    decode_base64url(payload.welcome, maximum=64 * 1024)
    try:
        await evict_channel_media_sessions(
            redis,
            settings,
            channel,
            conversation=conversation,
        )
    except MediaSessionRotationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "E2EE_MEDIA_ROTATION_UNAVAILABLE", "retry_after_ms": 2000},
            headers={"Retry-After": "2"},
        ) from exc
    channel.encryption_mode = "e2ee"
    channel.encryption_state = "active"
    channel.encryption_epoch = 1
    if initial_activation:
        channel.encryption_activated_at = datetime.now(UTC)
    e2ee = validate_e2ee_envelope(
        {
            "version": 2,
            "protocol": E2EE_PROTOCOL_MLS_10,
            "suite": E2EE_SUITE_MLS_128,
            "group_id": channel.encryption_group_id,
            "policy_generation": str(channel.encryption_policy_generation),
            "epoch": "1",
            "sender_device_id": payload.sender_device_id,
            "operation": "welcome",
            "ciphertext": payload.welcome,
        }
    )
    if e2ee is None:
        raise RuntimeError("validated MLS welcome disappeared")
    if conversation is not None:
        await lock_federated_dm_authority(session, conversation.authority_domain)
    message_id = await snowflake.mint()
    if conversation is not None:
        await admit_federated_dm_message(
            session,
            settings,
            conversation,
            message_id=message_id,
            message_domain=settings.domain,
            delta=dm_message_storage_delta(
                content=None,
                e2ee=e2ee,
                mention_user_refs=[],
                attachments=[],
            ),
        )
    message = Message(
        id=message_id,
        origin_domain=settings.domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=auth.user.id,
        author_domain=auth.user.origin_domain,
        content=None,
        e2ee=e2ee,
        encryption_policy_generation=channel.encryption_policy_generation,
        encryption_epoch=1,
        message_type=7,
        flags=4,
        mention_user_refs=[],
    )
    session.add(message)
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            mention_user_refs=[],
        )
    )
    channel.last_message_id = message.id
    channel.last_message_domain = message.origin_domain
    await session.flush()
    await publish_policy_update(session, redis, settings, access, auth.user)
    rendered_message = message_payload(message, auth.user, [])
    if access.guild is not None:
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            auth.user,
            "guild.message.create",
            {"message": rendered_message, "author": profile_from_user(auth.user)},
            channel=channel,
        )
    elif conversation is not None:
        event_type = (
            "dm.group.message.committed" if conversation.type == "group" else "dm.message.create"
        )
        event_content = {
            "message": rendered_message,
            "author": profile_from_user(auth.user),
            "encryption_policy": channel_encryption_policy_payload(channel),
        }
        envelope = await build_envelope(
            session,
            settings,
            event_type,
            auth.user,
            event_content,
            context=(
                {
                    "conversation_id": str(conversation.id),
                    "conversation_domain": conversation.origin_domain,
                    "state_version": str(conversation.state_version),
                }
                if conversation.type == "group"
                else None
            ),
            authority_attested_actor=auth.user.origin_domain != settings.domain,
        )
        for destination in {
            participant.origin_domain
            for participant in access.participants
            if participant.origin_domain != settings.domain
        }:
            await queue_event(session, settings, destination, envelope)
    await session.commit()
    if access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    rendered_channel = channel_payload(channel)
    await publish_channel_dispatch(redis, access, "CHANNEL_UPDATE", rendered_channel)
    await publish_channel_dispatch(redis, access, "MESSAGE_CREATE", rendered_message)
    return rendered_channel


@router.post("/channels/{channel_id}/rekey/propose")
async def propose_room_rekey(
    channel_id: EntityRef,
    payload: RoomProposalRequest,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    conversation = await require_room_policy_authority(
        session,
        redis,
        settings,
        access,
        auth.user,
        allow_remote_authority=True,
    )
    authority = room_authority_domain(settings, access, conversation)
    if authority != settings.domain:
        await require_active_sender_device(session, auth.user, payload.sender_device_id)
        return await proxy_room_e2ee_request(
            session,
            settings,
            authority,
            "/_kaede/v1/e2ee/rooms/rekey/propose",
            channel=access.channel,
            actor=auth.user,
            body={"sender_device_id": payload.sender_device_id},
        )
    access = await lock_local_channel_mutation(session, settings, access)
    await require_room_policy_authority(session, redis, settings, access, auth.user)
    if auth.user.origin_domain == settings.domain:
        await require_active_sender_device(session, auth.user, payload.sender_device_id)
    channel = access.channel
    if channel.encryption_mode != "e2ee" or channel.encryption_state not in {
        "active",
        "rekeying",
    }:
        raise HTTPException(status_code=409, detail={"code": "E2EE_POLICY_CONTEXT_MISMATCH"})
    participants = await room_participants(session, redis, access)
    packages = await claim_room_key_packages(
        session,
        settings,
        participants,
        claimant=auth.user,
        channel=channel,
        excluded_device_id=payload.sender_device_id,
    )
    if not packages:
        raise HTTPException(status_code=409, detail={"code": "E2EE_NO_OTHER_DEVICES"})
    proposal_id = secrets.token_urlsafe(32)
    group_id = encode_base64url(secrets.token_bytes(32))
    generation = channel.encryption_policy_generation + 1
    await redis.setex(
        f"e2ee:room-rekey:{proposal_id}",
        600,
        json.dumps(
            {
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "actor_id": str(auth.user.id),
                "actor_domain": auth.user.origin_domain,
                "sender_device_id": payload.sender_device_id,
                "previous_generation": str(channel.encryption_policy_generation),
                "generation": str(generation),
                "group_id": group_id,
            },
            separators=(",", ":"),
        ),
    )
    await session.commit()
    return {
        "proposal_id": proposal_id,
        "policy": {
            "mode": "e2ee",
            "state": "rekeying",
            "generation": str(generation),
            "protocol": E2EE_PROTOCOL_MLS_10,
            "suite": E2EE_SUITE_MLS_128,
            "group_id": group_id,
            "epoch": None,
        },
        "key_packages": packages,
    }


@router.post("/channels/{channel_id}/rekey/activate")
async def activate_room_rekey(
    channel_id: EntityRef,
    payload: RoomRekeyActivationRequest,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    access = await load_channel_access(session, settings, auth.user, channel_id)
    conversation = await require_room_policy_authority(
        session,
        redis,
        settings,
        access,
        auth.user,
        allow_remote_authority=True,
    )
    authority = room_authority_domain(settings, access, conversation)
    if authority != settings.domain:
        await require_active_sender_device(session, auth.user, payload.sender_device_id)
        rendered = await proxy_room_e2ee_request(
            session,
            settings,
            authority,
            "/_kaede/v1/e2ee/rooms/rekey/activate",
            channel=access.channel,
            actor=auth.user,
            body=payload.model_dump(),
        )
        await apply_remote_active_policy(session, access.channel, rendered)
        return rendered
    raw = await redis.get(f"e2ee:room-rekey:{payload.proposal_id}")
    if raw is None:
        raise HTTPException(status_code=409, detail={"code": "E2EE_REKEY_PROPOSAL_EXPIRED"})
    try:
        proposal = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=409, detail={"code": "E2EE_REKEY_PROPOSAL_EXPIRED"}
        ) from exc
    channel_numeric_id, channel_domain = channel_id.resolve(settings.domain)
    expected = {
        "channel_id": str(channel_numeric_id),
        "channel_domain": channel_domain,
        "actor_id": str(auth.user.id),
        "actor_domain": auth.user.origin_domain,
        "sender_device_id": payload.sender_device_id,
        "generation": payload.policy_generation,
    }
    if any(proposal.get(key) != value for key, value in expected.items()):
        raise HTTPException(status_code=409, detail={"code": "E2EE_POLICY_CONTEXT_MISMATCH"})
    access = await load_channel_access(session, settings, auth.user, channel_id)
    access = await lock_local_channel_mutation(session, settings, access)
    await require_room_policy_authority(session, redis, settings, access, auth.user)
    if auth.user.origin_domain == settings.domain:
        await require_active_sender_device(session, auth.user, payload.sender_device_id)
    channel = access.channel
    if (
        channel.encryption_mode != "e2ee"
        or channel.encryption_state not in {"active", "rekeying"}
        or str(channel.encryption_policy_generation) != proposal.get("previous_generation")
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_POLICY_CONTEXT_MISMATCH"})
    channel.encryption_state = "activating"
    channel.encryption_policy_generation = int(payload.policy_generation)
    channel.encryption_group_id = str(proposal["group_id"])
    channel.encryption_epoch = 1
    result = await activate_room_encryption(
        channel_id,
        RoomActivationRequest.model_validate(payload.model_dump(exclude={"proposal_id"})),
        auth,
        session,
        redis,
        snowflake,
        settings,
    )
    await redis.delete(f"e2ee:room-rekey:{payload.proposal_id}")
    return result
