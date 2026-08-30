from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Literal, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channels import load_webhook_capability_channel_access
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.e2ee import (
    RoomActivationRequest,
    RoomProposalRequest,
    activate_automation_room_encryption,
    propose_automation_room_encryption,
)
from app.api.webhooks import (
    _webhook_management_target,
    manageable_webhook,
    token_webhook,
    valid_webhook_name,
    webhook_message_admission_options,
)
from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.automation_e2ee import (
    automation_device_protocol_id,
    automation_device_registration_input,
    automation_key_package_upload_input,
    automation_mls_credential,
)
from app.chat.channel_access import ChannelAccess
from app.chat.e2ee import E2EE_SUITE_MLS_128, channel_encryption_policy_payload
from app.chat.e2ee_controls import e2ee_control_record_payload
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import (
    federation_channel_state,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import channel_payload
from app.chat.postcommit import publish_committed_dispatches
from app.chat.schemas import MessageCreate, RequestModel, cleaned_nonempty
from app.core.base64url import decode_base64url, encode_base64url
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef, Snowflake, WireSnowflake
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Channel,
    E2EEControlRecord,
    EncryptedForumStarterReservation,
    Guild,
    Message,
    User,
    Webhook,
    WebhookE2EEDevice,
    WebhookE2EEKeyPackage,
    WebhookE2EEParticipation,
)

router = APIRouter(tags=["webhook end-to-end encryption"])

WEBHOOK_E2EE_CAPABILITIES = frozenset({"e2ee-mls/1", "e2ee-media/1"})
WEBHOOK_E2EE_CHALLENGE_TTL_SECONDS = 300
WEBHOOK_E2EE_KEY_PACKAGE_MAX_LIFETIME = timedelta(days=30)
MAX_WEBHOOK_E2EE_KEY_PACKAGES = 100
MAX_WEBHOOK_E2EE_KEY_PACKAGE_BYTES = 32 * 1024
MAX_WEBHOOK_E2EE_CREDENTIAL_BYTES = 16 * 1024
WEBHOOK_E2EE_DEVICE_LIMIT = ClientRateLimit("webhook-e2ee-device", 20, 60)
WEBHOOK_E2EE_PACKAGE_LIMIT = ClientRateLimit("webhook-e2ee-package", 60, 60)
WEBHOOK_E2EE_AUDIT_ACTION_UPDATE_INTEGRATION = 81


class WebhookDeviceChallengeRequest(RequestModel):
    identity_key: str = Field(min_length=43, max_length=43)
    credential_digest: str = Field(min_length=43, max_length=43)

    @field_validator("identity_key", "credential_digest")
    @classmethod
    def canonical_32_bytes(cls, value: str) -> str:
        decode_base64url(value, size=32)
        return value


class WebhookDeviceRegisterRequest(RequestModel):
    challenge_id: str = Field(pattern=r"^kwec_[A-Za-z0-9_-]{32}$")
    identity_key: str = Field(min_length=43, max_length=43)
    credential: str = Field(min_length=2, max_length=22_000)
    signature: str = Field(min_length=86, max_length=86)
    capabilities: list[str] = Field(min_length=1, max_length=8)

    @field_validator("identity_key")
    @classmethod
    def canonical_identity_key(cls, value: str) -> str:
        decode_base64url(value, size=32)
        return value

    @field_validator("credential")
    @classmethod
    def bounded_credential(cls, value: str) -> str:
        decode_base64url(value, maximum=MAX_WEBHOOK_E2EE_CREDENTIAL_BYTES)
        return value

    @field_validator("signature")
    @classmethod
    def canonical_signature(cls, value: str) -> str:
        decode_base64url(value, size=64)
        return value

    @field_validator("capabilities")
    @classmethod
    def supported_capabilities(cls, value: list[str]) -> list[str]:
        if (
            len(value) != len(set(value))
            or not set(value) <= WEBHOOK_E2EE_CAPABILITIES
            or "e2ee-mls/1" not in value
        ):
            raise ValueError("webhook E2EE capabilities are invalid")
        return sorted(value)


class WebhookKeyPackageUploadRequest(RequestModel):
    cipher_suite: Literal["MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519"]
    expires_at: datetime
    packages: list[str] = Field(min_length=1, max_length=50)
    signature: str = Field(min_length=86, max_length=86)

    @field_validator("packages")
    @classmethod
    def bounded_packages(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("key packages must be unique")
        for package in value:
            if not decode_base64url(package, maximum=MAX_WEBHOOK_E2EE_KEY_PACKAGE_BYTES):
                raise ValueError("key packages cannot be empty")
        return value

    @field_validator("signature")
    @classmethod
    def canonical_signature(cls, value: str) -> str:
        decode_base64url(value, size=64)
        return value

    @model_validator(mode="after")
    def timezone_required(self) -> WebhookKeyPackageUploadRequest:
        if self.expires_at.tzinfo is None:
            raise ValueError("key package expiry requires a timezone")
        return self


class WebhookEncryptedForumReservationRequest(RequestModel):
    """Starterless shell used while the webhook device creates the child MLS group."""

    name: str = Field(min_length=1, max_length=100)
    applied_tag_ids: list[WireSnowflake] = Field(default_factory=list, max_length=5)
    client_nonce: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return cleaned_nonempty(value)


class WebhookEncryptedForumStarterRequest(RequestModel):
    e2ee: dict[str, object]
    client_nonce: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    attachment_ids: list[WireSnowflake] = Field(default_factory=list, max_length=10)
    username: str | None = None
    avatar_url: str | None = Field(default=None, max_length=2048, pattern=r"^https://")

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str | None) -> str | None:
        return valid_webhook_name(value) if value is not None else None


WebhookE2EEDeviceHeader = Annotated[
    str,
    Header(
        alias="X-Kaede-E2EE-Device",
        pattern=r"^kwe_[A-Za-z0-9_-]{43}$",
    ),
]


def webhook_e2ee_ref(webhook: Webhook) -> str:
    return f"{webhook.id}@{webhook.guild_domain}"


def webhook_device_protocol_id(webhook: Webhook, identity_key: bytes) -> str:
    return automation_device_protocol_id(
        namespace="kaede-webhook-e2ee-device-v1",
        prefix="kwe_",
        principal_ref=webhook_e2ee_ref(webhook),
        identity_key=identity_key,
    )


def webhook_mls_credential(webhook: Webhook, protocol_id: str) -> bytes:
    ref = webhook_e2ee_ref(webhook)
    return automation_mls_credential(
        account=f"webhook:{ref}",
        credential_type="kaede-webhook-device-v1",
        device_id=protocol_id,
        lineage={"webhook_ref": ref},
    )


def webhook_device_registration_input(
    webhook: Webhook,
    identity_key: bytes,
    credential_digest: bytes,
    challenge: bytes,
) -> bytes:
    return automation_device_registration_input(
        namespace="kaede-webhook-e2ee-device-registration-v1",
        principal_ref=webhook_e2ee_ref(webhook),
        identity_key=identity_key,
        credential_digest=credential_digest,
        challenge=challenge,
    )


def webhook_key_package_upload_input(
    device: WebhookE2EEDevice,
    *,
    cipher_suite: str,
    expires_at: datetime,
    package_hashes: list[bytes],
) -> bytes:
    return automation_key_package_upload_input(
        namespace="kaede-webhook-e2ee-key-packages-v1",
        protocol_id=device.protocol_id,
        generation=device.generation,
        cipher_suite=cipher_suite,
        expires_at=expires_at,
        package_hashes=package_hashes,
    )


def render_webhook_e2ee_device(
    device: WebhookE2EEDevice,
    webhook: Webhook,
    *,
    available_key_packages: int = 0,
) -> dict[str, object]:
    return {
        "webhook_ref": f"{device.webhook_id}@{device.webhook_domain}",
        "device_id": device.protocol_id,
        # The durable message row retains its creator FK for storage and
        # moderation, while the MLS credential above is the distinct webhook
        # automation identity. Clients bind rich AAD to this exact projection.
        "author_ref": f"{webhook.creator_id}@{webhook.creator_domain}",
        "identity_key": encode_base64url(device.identity_key),
        "credential": encode_base64url(device.credential),
        "capabilities": list(device.capabilities or []),
        "generation": str(device.generation),
        "trust_state": device.trust_state,
        "available_key_packages": available_key_packages,
    }


def _challenge_key(webhook: Webhook, challenge_id: str) -> str:
    return f"webhook:e2ee:device-challenge:{webhook.guild_domain}:{webhook.id}:{challenge_id}"


async def current_webhook_e2ee_device(
    session: AsyncSession,
    webhook: Webhook,
    protocol_id: str | None = None,
    *,
    for_update: bool = False,
) -> WebhookE2EEDevice:
    statement = select(WebhookE2EEDevice).where(
        WebhookE2EEDevice.webhook_id == webhook.id,
        WebhookE2EEDevice.webhook_domain == webhook.guild_domain,
        WebhookE2EEDevice.trust_state == "trusted",
        WebhookE2EEDevice.revoked_at.is_(None),
    )
    if protocol_id is not None:
        statement = statement.where(WebhookE2EEDevice.protocol_id == protocol_id)
    if for_update:
        statement = statement.with_for_update()
    device = await session.scalar(statement)
    if device is None:
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_E2EE_DEVICE_NOT_FOUND"})
    return device


async def require_webhook_e2ee_participation(
    session: AsyncSession,
    webhook: Webhook,
    channel: Channel,
    protocol_id: str | None,
) -> tuple[WebhookE2EEParticipation, WebhookE2EEDevice]:
    if protocol_id is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "WEBHOOK_E2EE_PARTICIPANT_REQUIRED"},
        )
    row = (
        await session.execute(
            select(WebhookE2EEParticipation, WebhookE2EEDevice)
            .join(
                WebhookE2EEDevice,
                WebhookE2EEDevice.id == WebhookE2EEParticipation.device_id,
            )
            .where(
                WebhookE2EEParticipation.webhook_id == webhook.id,
                WebhookE2EEParticipation.webhook_domain == webhook.guild_domain,
                WebhookE2EEParticipation.channel_id == channel.id,
                WebhookE2EEParticipation.channel_domain == channel.origin_domain,
                WebhookE2EEParticipation.status == "active",
                WebhookE2EEDevice.protocol_id == protocol_id,
                WebhookE2EEDevice.trust_state == "trusted",
                WebhookE2EEDevice.revoked_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "WEBHOOK_E2EE_PARTICIPANT_REQUIRED"},
        )
    if channel.encryption_mode != "e2ee" or channel.encryption_state != "active":
        raise HTTPException(status_code=409, detail={"code": "E2EE_REKEY_REQUIRED"})
    return cast(tuple[WebhookE2EEParticipation, WebhookE2EEDevice], row)


async def claim_webhook_e2ee_key_packages(
    session: AsyncSession,
    channel: Channel,
    *,
    operation_id: str,
    operation_domain: str,
    max_devices: int,
    excluded_device_id: str | None = None,
) -> list[dict[str, str]]:
    """Claim one package for every manager-approved webhook automation device."""

    rows = list(
        (
            await session.execute(
                select(WebhookE2EEParticipation, WebhookE2EEDevice, Webhook)
                .join(
                    WebhookE2EEDevice,
                    WebhookE2EEDevice.id == WebhookE2EEParticipation.device_id,
                )
                .join(
                    Webhook,
                    (Webhook.id == WebhookE2EEParticipation.webhook_id)
                    & (Webhook.guild_domain == WebhookE2EEParticipation.webhook_domain),
                )
                .where(
                    WebhookE2EEParticipation.channel_id == channel.id,
                    WebhookE2EEParticipation.channel_domain == channel.origin_domain,
                    WebhookE2EEParticipation.status.in_(("pending", "active")),
                    WebhookE2EEDevice.trust_state == "trusted",
                    WebhookE2EEDevice.revoked_at.is_(None),
                    Webhook.revoked_at.is_(None),
                )
                .order_by(WebhookE2EEDevice.protocol_id)
            )
        ).tuples()
    )
    rows = [row for row in rows if row[1].protocol_id != excluded_device_id]
    if len(rows) > max_devices:
        raise HTTPException(status_code=409, detail={"code": "E2EE_ROOM_DEVICE_LIMIT"})
    now = datetime.now(UTC)
    claimed: list[dict[str, str]] = []
    for _participation, device, webhook in rows:
        package = await session.scalar(
            select(WebhookE2EEKeyPackage)
            .where(
                WebhookE2EEKeyPackage.device_id == device.id,
                WebhookE2EEKeyPackage.claimed_operation_id == operation_id,
                WebhookE2EEKeyPackage.claimed_operation_domain == operation_domain,
            )
            .limit(1)
        )
        if package is None:
            package = await session.scalar(
                select(WebhookE2EEKeyPackage)
                .where(
                    WebhookE2EEKeyPackage.device_id == device.id,
                    WebhookE2EEKeyPackage.claimed_at.is_(None),
                    WebhookE2EEKeyPackage.expires_at > now,
                    WebhookE2EEKeyPackage.cipher_suite == E2EE_SUITE_MLS_128,
                )
                .order_by(
                    WebhookE2EEKeyPackage.expires_at,
                    WebhookE2EEKeyPackage.created_at,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if package is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "E2EE_KEY_PACKAGE_UNAVAILABLE",
                        "device_id": device.protocol_id,
                    },
                )
            package.claimed_at = now
            package.claimed_operation_id = operation_id
            package.claimed_operation_domain = operation_domain
        claimed.append(
            {
                # The proposal wire schema historically called this a user
                # ref. For automation principals it is the authority-scoped
                # webhook ref; the exact credential branch authenticates it.
                "user_id": str(webhook.id),
                "user_domain": webhook.guild_domain,
                "device_id": device.protocol_id,
                "identity_key": encode_base64url(device.identity_key),
                "credential": encode_base64url(device.credential),
                "key_package": encode_base64url(package.package),
            }
        )
    return claimed


async def webhook_e2ee_target_channel(
    session: AsyncSession,
    settings: Settings,
    webhook: Webhook,
    channel_ref: EntityRef,
) -> Channel:
    access = await load_webhook_capability_channel_access(
        session,
        settings,
        channel_ref,
        webhook_channel_id=webhook.channel_id,
        webhook_channel_domain=webhook.channel_domain,
    )
    return access.channel


async def _webhook_forum_reservation_access(
    session: AsyncSession,
    settings: Settings,
    webhook: Webhook,
    thread_ref: EntityRef,
    device_id: str,
) -> tuple[
    ChannelAccess,
    EncryptedForumStarterReservation,
    WebhookE2EEParticipation,
    WebhookE2EEDevice,
    User,
]:
    """Authorize the exact token/device pair bound into a forum reservation."""

    access = await load_webhook_capability_channel_access(
        session,
        settings,
        thread_ref,
        webhook_channel_id=webhook.channel_id,
        webhook_channel_domain=webhook.channel_domain,
    )
    thread = access.channel
    reservation = await session.scalar(
        select(EncryptedForumStarterReservation).where(
            EncryptedForumStarterReservation.thread_id == thread.id,
            EncryptedForumStarterReservation.thread_domain == thread.origin_domain,
        )
    )
    creator = await session.get(User, (webhook.creator_id, webhook.creator_domain))
    device = await current_webhook_e2ee_device(session, webhook, device_id)
    participation = await session.scalar(
        select(WebhookE2EEParticipation).where(
            WebhookE2EEParticipation.webhook_id == webhook.id,
            WebhookE2EEParticipation.webhook_domain == webhook.guild_domain,
            WebhookE2EEParticipation.channel_id == thread.id,
            WebhookE2EEParticipation.channel_domain == thread.origin_domain,
            WebhookE2EEParticipation.device_id == device.id,
            WebhookE2EEParticipation.status.in_(("pending", "active")),
        )
    )
    if (
        reservation is None
        or creator is None
        or participation is None
        or thread.type not in {10, 11, 12}
        or not thread.e2ee_required
        or (reservation.parent_id, reservation.parent_domain)
        != (webhook.channel_id, webhook.channel_domain)
        or reservation.claimant_kind != "webhook"
        or (reservation.claimant_id, reservation.claimant_domain)
        != (creator.id, creator.origin_domain)
        or (reservation.webhook_id, reservation.webhook_domain)
        != (webhook.id, webhook.guild_domain)
        or reservation.claimant_device_id != device.protocol_id
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "STARTER_RESERVATION_NOT_OWNED"},
        )
    return access, reservation, participation, device, creator


def _webhook_automation_auth(creator: User) -> AuthenticatedUser:
    # Message/thread services only consume the durable actor here. The token,
    # destination, reservation and MLS device were authorized independently.
    return cast(AuthenticatedUser, SimpleNamespace(user=creator))


@router.post(
    "/api/v1/webhooks/{webhook_id}/{path_token}/e2ee/devices/challenge",
    status_code=201,
)
async def create_webhook_e2ee_device_challenge(
    webhook_id: Snowflake,
    path_token: str,
    payload: WebhookDeviceChallengeRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token)
    await enforce_keyed_rate_limit(
        redis,
        response,
        WEBHOOK_E2EE_DEVICE_LIMIT,
        identity=f"{webhook.guild_domain}:{webhook.id}",
    )
    identity_key = decode_base64url(payload.identity_key, size=32)
    credential_digest = decode_base64url(payload.credential_digest, size=32)
    challenge_id = f"kwec_{secrets.token_urlsafe(24)}"
    challenge = secrets.token_bytes(32)
    signing_input = webhook_device_registration_input(
        webhook,
        identity_key,
        credential_digest,
        challenge,
    )
    await redis.setex(
        _challenge_key(webhook, challenge_id),
        WEBHOOK_E2EE_CHALLENGE_TTL_SECONDS,
        json.dumps(
            {
                "webhook_ref": webhook_e2ee_ref(webhook),
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
        "expires_in": WEBHOOK_E2EE_CHALLENGE_TTL_SECONDS,
        "webhook_ref": webhook_e2ee_ref(webhook),
    }


@router.post("/api/v1/webhooks/{webhook_id}/{path_token}/e2ee/devices", status_code=201)
async def register_webhook_e2ee_device(
    webhook_id: Snowflake,
    path_token: str,
    payload: WebhookDeviceRegisterRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token, for_update=True)
    raw_challenge = await redis.getdel(_challenge_key(webhook, payload.challenge_id))
    if not isinstance(raw_challenge, str):
        raise HTTPException(
            status_code=409,
            detail={"code": "WEBHOOK_E2EE_DEVICE_CHALLENGE_EXPIRED"},
        )
    try:
        challenge = json.loads(raw_challenge)
    except json.JSONDecodeError as exc:
        raise RuntimeError("stored webhook E2EE challenge is corrupt") from exc
    identity_key = decode_base64url(payload.identity_key, size=32)
    credential = decode_base64url(payload.credential, maximum=MAX_WEBHOOK_E2EE_CREDENTIAL_BYTES)
    expected_challenge = {
        "webhook_ref": webhook_e2ee_ref(webhook),
        "identity_key": payload.identity_key,
        "credential_digest": encode_base64url(hashlib.sha256(credential).digest()),
        "signing_input": challenge.get("signing_input"),
    }
    if challenge != expected_challenge:
        raise HTTPException(
            status_code=409,
            detail={"code": "WEBHOOK_E2EE_DEVICE_CHALLENGE_MISMATCH"},
        )
    signing_input = decode_base64url(str(challenge["signing_input"]), maximum=1024)
    try:
        Ed25519PublicKey.from_public_bytes(identity_key).verify(
            decode_base64url(payload.signature, size=64),
            signing_input,
        )
    except (InvalidSignature, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "WEBHOOK_E2EE_DEVICE_PROOF_INVALID"},
        ) from exc
    protocol_id = webhook_device_protocol_id(webhook, identity_key)
    expected_credential = webhook_mls_credential(webhook, protocol_id)
    if not secrets.compare_digest(credential, expected_credential):
        raise HTTPException(
            status_code=400,
            detail={"code": "WEBHOOK_E2EE_DEVICE_CREDENTIAL_INVALID"},
        )
    existing = await session.scalar(
        select(WebhookE2EEDevice)
        .where(
            WebhookE2EEDevice.webhook_id == webhook.id,
            WebhookE2EEDevice.webhook_domain == webhook.guild_domain,
            WebhookE2EEDevice.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if existing is not None:
        if (
            existing.protocol_id != protocol_id
            or not secrets.compare_digest(existing.identity_key, identity_key)
            or not secrets.compare_digest(existing.credential, credential)
            or set(existing.capabilities or []) != set(payload.capabilities)
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "WEBHOOK_E2EE_DEVICE_EXISTS"},
            )
        return render_webhook_e2ee_device(existing, webhook)
    generation = (
        int(
            await session.scalar(
                select(func.coalesce(func.max(WebhookE2EEDevice.generation), 0)).where(
                    WebhookE2EEDevice.webhook_id == webhook.id,
                    WebhookE2EEDevice.webhook_domain == webhook.guild_domain,
                )
            )
            or 0
        )
        + 1
    )
    device = WebhookE2EEDevice(
        id=await snowflake.mint(),
        webhook_id=webhook.id,
        webhook_domain=webhook.guild_domain,
        protocol_id=protocol_id,
        identity_key=identity_key,
        credential=credential,
        capabilities=list(payload.capabilities),
        generation=generation,
        trust_state="trusted",
    )
    session.add(device)
    await session.commit()
    return render_webhook_e2ee_device(device, webhook)


@router.get("/api/v1/webhooks/{webhook_id}/{path_token}/e2ee/devices")
async def list_webhook_e2ee_devices(
    webhook_id: Snowflake,
    path_token: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token)
    devices = list(
        await session.scalars(
            select(WebhookE2EEDevice)
            .where(
                WebhookE2EEDevice.webhook_id == webhook.id,
                WebhookE2EEDevice.webhook_domain == webhook.guild_domain,
            )
            .order_by(WebhookE2EEDevice.generation)
        )
    )
    counts = {
        int(device_id): int(count)
        for device_id, count in (
            await session.execute(
                select(
                    WebhookE2EEKeyPackage.device_id,
                    func.count(WebhookE2EEKeyPackage.id),
                )
                .where(
                    WebhookE2EEKeyPackage.claimed_at.is_(None),
                    WebhookE2EEKeyPackage.expires_at > datetime.now(UTC),
                )
                .group_by(WebhookE2EEKeyPackage.device_id)
            )
        ).tuples()
    }
    return {
        "webhook_ref": webhook_e2ee_ref(webhook),
        "devices": [
            render_webhook_e2ee_device(
                device,
                webhook,
                available_key_packages=counts.get(device.id, 0),
            )
            for device in devices
        ],
    }


@router.post(
    "/api/v1/webhooks/{webhook_id}/{path_token}/e2ee/devices/{protocol_id}/key-packages",
    status_code=201,
)
async def upload_webhook_e2ee_key_packages(
    webhook_id: Snowflake,
    path_token: str,
    protocol_id: str,
    payload: WebhookKeyPackageUploadRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token, for_update=True)
    await enforce_keyed_rate_limit(
        redis,
        response,
        WEBHOOK_E2EE_PACKAGE_LIMIT,
        identity=f"{webhook.guild_domain}:{webhook.id}:{protocol_id}",
    )
    device = await current_webhook_e2ee_device(
        session,
        webhook,
        protocol_id,
        for_update=True,
    )
    now = datetime.now(UTC)
    expires_at = payload.expires_at.astimezone(UTC)
    if (
        expires_at <= now + timedelta(minutes=5)
        or expires_at > now + WEBHOOK_E2EE_KEY_PACKAGE_MAX_LIFETIME
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "WEBHOOK_E2EE_KEY_PACKAGE_EXPIRY_INVALID"},
        )
    decoded = [
        decode_base64url(item, maximum=MAX_WEBHOOK_E2EE_KEY_PACKAGE_BYTES)
        for item in payload.packages
    ]
    package_hashes = [hashlib.sha256(item).digest() for item in decoded]
    signing_input = webhook_key_package_upload_input(
        device,
        cipher_suite=payload.cipher_suite,
        expires_at=expires_at,
        package_hashes=package_hashes,
    )
    try:
        Ed25519PublicKey.from_public_bytes(device.identity_key).verify(
            decode_base64url(payload.signature, size=64),
            signing_input,
        )
    except (InvalidSignature, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "WEBHOOK_E2EE_KEY_PACKAGE_PROOF_INVALID"},
        ) from exc
    existing = list(
        await session.scalars(
            select(WebhookE2EEKeyPackage).where(
                WebhookE2EEKeyPackage.device_id == device.id,
                WebhookE2EEKeyPackage.package_hash.in_(package_hashes),
            )
        )
    )
    if any(item.claimed_at is not None for item in existing):
        raise HTTPException(
            status_code=409,
            detail={"code": "WEBHOOK_E2EE_KEY_PACKAGE_REUSE"},
        )
    available = int(
        await session.scalar(
            select(func.count(WebhookE2EEKeyPackage.id)).where(
                WebhookE2EEKeyPackage.device_id == device.id,
                WebhookE2EEKeyPackage.claimed_at.is_(None),
                WebhookE2EEKeyPackage.expires_at > now,
            )
        )
        or 0
    )
    existing_hashes = {item.package_hash for item in existing}
    new_packages = [
        (package, package_hash)
        for package, package_hash in zip(decoded, package_hashes, strict=True)
        if package_hash not in existing_hashes
    ]
    if available + len(new_packages) > MAX_WEBHOOK_E2EE_KEY_PACKAGES:
        raise HTTPException(status_code=409, detail={"code": "WEBHOOK_E2EE_KEY_PACKAGE_LIMIT"})
    for package, package_hash in new_packages:
        session.add(
            WebhookE2EEKeyPackage(
                id=await snowflake.mint(),
                device_id=device.id,
                cipher_suite=E2EE_SUITE_MLS_128,
                package=package,
                package_hash=package_hash,
                expires_at=expires_at,
            )
        )
    await session.commit()
    return {
        "device_id": device.protocol_id,
        "accepted": len(new_packages),
        "available_key_packages": available + len(new_packages),
    }


@router.post(
    "/api/v1/webhooks/{webhook_id}/{path_token}/e2ee/forum-reservations",
    status_code=201,
)
async def create_webhook_encrypted_forum_reservation(
    webhook_id: Snowflake,
    path_token: str,
    payload: WebhookEncryptedForumReservationRequest,
    e2ee_device_id: WebhookE2EEDeviceHeader,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Reserve a starterless forum child for client-side MLS activation."""

    from app.api.threads import ThreadCreate, create_thread_service

    webhook = await token_webhook(session, int(webhook_id), path_token)
    parent = await webhook_e2ee_target_channel(
        session,
        settings,
        webhook,
        EntityRef(f"{webhook.channel_id}@{webhook.channel_domain}"),
    )
    if parent.type != 15 or not parent.e2ee_required:
        raise HTTPException(
            status_code=409,
            detail={"code": "WEBHOOK_E2EE_FORUM_REQUIRED"},
        )
    parent_participation, device = await require_webhook_e2ee_participation(
        session,
        webhook,
        parent,
        e2ee_device_id,
    )
    creator = await session.get(User, (webhook.creator_id, webhook.creator_domain))
    if creator is None:
        raise HTTPException(status_code=410, detail={"code": "WEBHOOK_CREATOR_MISSING"})
    rendered = await create_thread_service(
        EntityRef(f"{parent.id}@{parent.origin_domain}"),
        ThreadCreate(
            name=payload.name,
            applied_tag_ids=[str(item) for item in payload.applied_tag_ids],
            starter_reservation_nonce=payload.client_nonce,
        ),
        _webhook_automation_auth(creator),
        session,
        redis,
        snowflake,
        settings,
        starter_admission_options=webhook_message_admission_options(
            webhook,
            creator,
            settings,
            device_id=device.protocol_id,
            avatar_hash=webhook.avatar_hash,
        ),
        starter_claimant_device_id=device.protocol_id,
    )
    raw_thread_id = rendered.get("id")
    raw_thread_domain = rendered.get("origin_domain")
    if not str(raw_thread_id).isdigit() or not isinstance(raw_thread_domain, str):
        raise RuntimeError("encrypted forum reservation omitted its thread identity")
    thread = await session.get(Channel, (int(str(raw_thread_id)), raw_thread_domain))
    if thread is None:
        raise RuntimeError("encrypted forum reservation thread disappeared")
    child_participation = await session.scalar(
        select(WebhookE2EEParticipation)
        .where(
            WebhookE2EEParticipation.webhook_id == webhook.id,
            WebhookE2EEParticipation.webhook_domain == webhook.guild_domain,
            WebhookE2EEParticipation.channel_id == thread.id,
            WebhookE2EEParticipation.channel_domain == thread.origin_domain,
            WebhookE2EEParticipation.device_id == device.id,
        )
        .with_for_update()
    )
    if child_participation is None:
        child_participation = WebhookE2EEParticipation(
            id=await snowflake.mint(),
            webhook_id=webhook.id,
            webhook_domain=webhook.guild_domain,
            channel_id=thread.id,
            channel_domain=thread.origin_domain,
            device_id=device.id,
            consenting_actor_id=parent_participation.consenting_actor_id,
            consenting_actor_domain=parent_participation.consenting_actor_domain,
            consent_generation=parent_participation.consent_generation,
            joined_epoch=0,
            history_floor_message_id=None,
            history_floor_message_domain=None,
            status="pending",
        )
        session.add(child_participation)
        await session.commit()
    elif child_participation.status not in {"pending", "active"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "WEBHOOK_E2EE_PARTICIPATION_REVOKED"},
        )
    rendered["webhook_e2ee"] = {
        "device_id": device.protocol_id,
        "status": child_participation.status,
    }
    return rendered


@router.post("/api/v1/webhooks/{webhook_id}/{path_token}/e2ee/channels/{thread_ref}/propose")
async def propose_webhook_encrypted_forum_room(
    webhook_id: Snowflake,
    path_token: str,
    thread_ref: EntityRef,
    payload: RoomProposalRequest,
    e2ee_device_id: WebhookE2EEDeviceHeader,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if payload.sender_device_id != e2ee_device_id:
        raise HTTPException(status_code=403, detail={"code": "WEBHOOK_E2EE_DEVICE_MISMATCH"})
    webhook = await token_webhook(session, int(webhook_id), path_token)
    (
        access,
        _reservation,
        _participation,
        _device,
        creator,
    ) = await _webhook_forum_reservation_access(
        session,
        settings,
        webhook,
        thread_ref,
        e2ee_device_id,
    )
    return await propose_automation_room_encryption(
        access,
        payload,
        _webhook_automation_auth(creator),
        session,
        redis,
        settings,
    )


@router.post("/api/v1/webhooks/{webhook_id}/{path_token}/e2ee/channels/{thread_ref}/activate")
async def activate_webhook_encrypted_forum_room(
    webhook_id: Snowflake,
    path_token: str,
    thread_ref: EntityRef,
    payload: RoomActivationRequest,
    e2ee_device_id: WebhookE2EEDeviceHeader,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if payload.sender_device_id != e2ee_device_id:
        raise HTTPException(status_code=403, detail={"code": "WEBHOOK_E2EE_DEVICE_MISMATCH"})
    webhook = await token_webhook(session, int(webhook_id), path_token)
    access, _reservation, _participation, device, creator = await _webhook_forum_reservation_access(
        session,
        settings,
        webhook,
        thread_ref,
        e2ee_device_id,
    )
    expected_digest = encode_base64url(hashlib.sha256(device.credential).digest())
    if (
        payload.prepared_vault_revision != str(device.generation)
        or payload.prepared_vault_digest != expected_digest
        or payload.vault_lease_token is not None
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "WEBHOOK_E2EE_DEVICE_STATE_MISMATCH"},
        )
    return await activate_automation_room_encryption(
        access,
        payload,
        _webhook_automation_auth(creator),
        session,
        redis,
        snowflake,
        settings,
    )


@router.post(
    "/api/v1/webhooks/{webhook_id}/{path_token}/e2ee/channels/{thread_ref}/starter",
    status_code=201,
)
async def claim_webhook_encrypted_forum_starter(
    webhook_id: Snowflake,
    path_token: str,
    thread_ref: EntityRef,
    payload: WebhookEncryptedForumStarterRequest,
    e2ee_device_id: WebhookE2EEDeviceHeader,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    from app.api.threads import claim_encrypted_forum_starter_service

    webhook = await token_webhook(session, int(webhook_id), path_token)
    access, _reservation, participation, device, creator = await _webhook_forum_reservation_access(
        session,
        settings,
        webhook,
        thread_ref,
        e2ee_device_id,
    )
    if participation.status != "active":
        raise HTTPException(status_code=409, detail={"code": "E2EE_REKEY_REQUIRED"})
    return await claim_encrypted_forum_starter_service(
        EntityRef(f"{access.channel.id}@{access.channel.origin_domain}"),
        MessageCreate(
            e2ee=payload.e2ee,
            client_nonce=payload.client_nonce,
            attachment_ids=[str(item) for item in payload.attachment_ids],
        ),
        _webhook_automation_auth(creator),
        session,
        redis,
        snowflake,
        settings,
        admission_options=webhook_message_admission_options(
            webhook,
            creator,
            settings,
            device_id=device.protocol_id,
            name=payload.username,
            avatar_hash=None if payload.avatar_url else webhook.avatar_hash,
            avatar_url=payload.avatar_url,
        ),
        claimant_device_id=device.protocol_id,
    )


def render_webhook_e2ee_participation(
    webhook: Webhook,
    channel: Channel,
    rows: list[tuple[WebhookE2EEParticipation, WebhookE2EEDevice]],
) -> dict[str, object]:
    return {
        "webhook_ref": webhook_e2ee_ref(webhook),
        "channel_ref": f"{channel.id}@{channel.origin_domain}",
        "devices": [
            {
                "device_id": device.protocol_id,
                "status": participation.status,
                "consent_generation": str(participation.consent_generation),
                "joined_epoch": str(participation.joined_epoch),
                "history_floor_message_ref": (
                    f"{participation.history_floor_message_id}@"
                    f"{participation.history_floor_message_domain}"
                    if participation.history_floor_message_id is not None
                    else None
                ),
            }
            for participation, device in rows
        ],
        "encryption_policy": channel_encryption_policy_payload(channel),
    }


async def webhook_e2ee_participation_state(
    session: AsyncSession,
    webhook: Webhook,
    channel: Channel,
) -> dict[str, object]:
    rows = list(
        (
            await session.execute(
                select(WebhookE2EEParticipation, WebhookE2EEDevice)
                .join(
                    WebhookE2EEDevice,
                    WebhookE2EEDevice.id == WebhookE2EEParticipation.device_id,
                )
                .where(
                    WebhookE2EEParticipation.webhook_id == webhook.id,
                    WebhookE2EEParticipation.webhook_domain == webhook.guild_domain,
                    WebhookE2EEParticipation.channel_id == channel.id,
                    WebhookE2EEParticipation.channel_domain == channel.origin_domain,
                )
                .order_by(WebhookE2EEDevice.generation)
            )
        ).tuples()
    )
    return render_webhook_e2ee_participation(webhook, channel, rows)


async def _queue_webhook_e2ee_policy_change(
    session: AsyncSession,
    settings: Settings,
    webhook: Webhook,
    channel: Channel,
    actor: User,
) -> Guild | None:
    if channel.encryption_mode != "e2ee" or channel.encryption_state != "active":
        return None
    guild = await session.get(Guild, (webhook.guild_id, webhook.guild_domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    channel.encryption_state = "rekeying"
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.channel.update",
        {"channel": federation_channel_state(channel)},
        channel=channel,
    )
    return guild


async def _publish_webhook_e2ee_policy_change(
    session: AsyncSession,
    redis: Redis,
    guild: Guild | None,
    channel: Channel,
) -> None:
    if guild is None:
        return
    await materialize_updated_at(session, channel)
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "CHANNEL_UPDATE",
        channel_payload(channel),
    )


async def grant_webhook_e2ee_participation_local(
    webhook_id: int,
    channel_ref: EntityRef,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    reason: str | None = None,
) -> dict[str, object]:
    webhook = await manageable_webhook(session, redis, settings, auth, webhook_id)
    channel = await webhook_e2ee_target_channel(session, settings, webhook, channel_ref)
    device = await current_webhook_e2ee_device(session, webhook, for_update=True)
    participation = await session.scalar(
        select(WebhookE2EEParticipation)
        .where(
            WebhookE2EEParticipation.webhook_id == webhook.id,
            WebhookE2EEParticipation.webhook_domain == webhook.guild_domain,
            WebhookE2EEParticipation.channel_id == channel.id,
            WebhookE2EEParticipation.channel_domain == channel.origin_domain,
            WebhookE2EEParticipation.device_id == device.id,
        )
        .with_for_update()
    )
    if participation is None:
        participation = WebhookE2EEParticipation(
            id=await snowflake.mint(),
            webhook_id=webhook.id,
            webhook_domain=webhook.guild_domain,
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            device_id=device.id,
            consenting_actor_id=auth.user.id,
            consenting_actor_domain=auth.user.origin_domain,
            consent_generation=1,
            joined_epoch=0,
            history_floor_message_id=channel.last_message_id,
            history_floor_message_domain=channel.last_message_domain,
            status="pending",
        )
        session.add(participation)
    elif participation.status == "revoked":
        participation.status = "pending"
        participation.revoked_at = None
        participation.consent_generation += 1
        participation.joined_epoch = 0
        participation.history_floor_message_id = channel.last_message_id
        participation.history_floor_message_domain = channel.last_message_domain
        participation.consenting_actor_id = auth.user.id
        participation.consenting_actor_domain = auth.user.origin_domain
    guild = await _queue_webhook_e2ee_policy_change(
        session,
        settings,
        webhook,
        channel,
        auth.user,
    )
    audit_guild = guild or await session.get(Guild, (webhook.guild_id, webhook.guild_domain))
    if audit_guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await add_audit_entry(
        session,
        snowflake,
        audit_guild,
        auth.user,
        WEBHOOK_E2EE_AUDIT_ACTION_UPDATE_INTEGRATION,
        target_type="webhook",
        target_ref={"id": str(webhook.id), "domain": webhook.guild_domain},
        reason=normalize_audit_reason(reason),
        changes=[{"key": "e2ee_participation", "new_value": "pending"}],
    )
    await session.commit()
    await _publish_webhook_e2ee_policy_change(session, redis, guild, channel)
    return await webhook_e2ee_participation_state(session, webhook, channel)


async def revoke_webhook_e2ee_participation_local(
    webhook_id: int,
    channel_ref: EntityRef,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    reason: str | None = None,
) -> dict[str, object]:
    webhook = await manageable_webhook(session, redis, settings, auth, webhook_id)
    channel = await webhook_e2ee_target_channel(session, settings, webhook, channel_ref)
    rows = list(
        await session.scalars(
            select(WebhookE2EEParticipation)
            .where(
                WebhookE2EEParticipation.webhook_id == webhook.id,
                WebhookE2EEParticipation.webhook_domain == webhook.guild_domain,
                WebhookE2EEParticipation.channel_id == channel.id,
                WebhookE2EEParticipation.channel_domain == channel.origin_domain,
                WebhookE2EEParticipation.status.in_(("pending", "active")),
            )
            .with_for_update()
        )
    )
    now = datetime.now(UTC)
    for participation in rows:
        participation.status = "revoked"
        participation.revoked_at = now
        participation.consent_generation += 1
    guild = await _queue_webhook_e2ee_policy_change(
        session,
        settings,
        webhook,
        channel,
        auth.user,
    )
    audit_guild = guild or await session.get(Guild, (webhook.guild_id, webhook.guild_domain))
    if audit_guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await add_audit_entry(
        session,
        snowflake,
        audit_guild,
        auth.user,
        WEBHOOK_E2EE_AUDIT_ACTION_UPDATE_INTEGRATION,
        target_type="webhook",
        target_ref={"id": str(webhook.id), "domain": webhook.guild_domain},
        reason=normalize_audit_reason(reason),
        changes=[{"key": "e2ee_participation", "new_value": "revoked"}],
    )
    await session.commit()
    await _publish_webhook_e2ee_policy_change(session, redis, guild, channel)
    return await webhook_e2ee_participation_state(session, webhook, channel)


async def get_webhook_e2ee_participation_local(
    webhook_id: int,
    channel_ref: EntityRef,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> dict[str, object]:
    webhook = await manageable_webhook(session, redis, settings, auth, webhook_id)
    channel = await webhook_e2ee_target_channel(session, settings, webhook, channel_ref)
    return await webhook_e2ee_participation_state(session, webhook, channel)


async def _route_managed_webhook_e2ee_participation(
    operation: Literal[
        "webhook.e2ee.get",
        "webhook.e2ee.grant",
        "webhook.e2ee.revoke",
    ],
    webhook_ref: EntityRef,
    channel_ref: EntityRef,
    guild_ref: EntityRef | None,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    reason: str | None,
) -> dict[str, object]:
    local_id, proxied = await _webhook_management_target(
        session,
        settings,
        auth,
        webhook_ref,
        guild_ref,
        operation,
        {
            "data": {"channel_ref": str(channel_ref)},
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied is not None:
        return cast(dict[str, object], proxied.body)
    if operation == "webhook.e2ee.get":
        return await get_webhook_e2ee_participation_local(
            local_id,
            channel_ref,
            auth,
            session,
            redis,
            settings,
        )
    if operation == "webhook.e2ee.grant":
        return await grant_webhook_e2ee_participation_local(
            local_id,
            channel_ref,
            auth,
            session,
            redis,
            snowflake,
            settings,
            reason,
        )
    return await revoke_webhook_e2ee_participation_local(
        local_id,
        channel_ref,
        auth,
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/api/v1/webhooks/{webhook_ref}/e2ee/channels/{channel_ref}")
async def get_webhook_e2ee_participation(
    webhook_ref: EntityRef,
    channel_ref: EntityRef,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await _route_managed_webhook_e2ee_participation(
        "webhook.e2ee.get",
        webhook_ref,
        channel_ref,
        guild_ref,
        auth,
        session,
        redis,
        snowflake,
        settings,
        None,
    )


@router.put("/api/v1/webhooks/{webhook_ref}/e2ee/channels/{channel_ref}")
async def grant_webhook_e2ee_participation(
    webhook_ref: EntityRef,
    channel_ref: EntityRef,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    return await _route_managed_webhook_e2ee_participation(
        "webhook.e2ee.grant",
        webhook_ref,
        channel_ref,
        guild_ref,
        auth,
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/api/v1/webhooks/{webhook_ref}/e2ee/channels/{channel_ref}")
async def revoke_webhook_e2ee_participation(
    webhook_ref: EntityRef,
    channel_ref: EntityRef,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    return await _route_managed_webhook_e2ee_participation(
        "webhook.e2ee.revoke",
        webhook_ref,
        channel_ref,
        guild_ref,
        auth,
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/api/v1/webhooks/{webhook_id}/{path_token}/e2ee/channels/{channel_ref}/control-log")
async def webhook_e2ee_control_log(
    webhook_id: Snowflake,
    path_token: str,
    channel_ref: EntityRef,
    after: EntityRef | None = None,
    limit: int = Query(default=25, ge=1, le=25),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token)
    channel = await webhook_e2ee_target_channel(session, settings, webhook, channel_ref)
    participation, device = await require_webhook_e2ee_participation(
        session,
        webhook,
        channel,
        e2ee_device_id,
    )
    conditions = [
        E2EEControlRecord.channel_id == channel.id,
        E2EEControlRecord.channel_domain == channel.origin_domain,
        E2EEControlRecord.id >= channel.created_floor_id,
        E2EEControlRecord.origin_domain == webhook.guild_domain,
        E2EEControlRecord.room_operation_id.is_not(None),
        E2EEControlRecord.room_operation_domain == webhook.guild_domain,
    ]
    if participation.history_floor_message_id is not None:
        floor = await session.get(
            Message,
            (
                participation.history_floor_message_id,
                participation.history_floor_message_domain,
            ),
        )
        if floor is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_HISTORY_FLOOR_INVALID"},
            )
        conditions.append(E2EEControlRecord.id > floor.id)
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
        if after_domain != webhook.guild_domain:
            raise HTTPException(
                status_code=422,
                detail={"code": "E2EE_CONTROL_CURSOR_INVALID"},
            )
        conditions.append(E2EEControlRecord.id > after_id)
    candidates = list(
        await session.scalars(
            select(E2EEControlRecord)
            .where(*conditions)
            .order_by(E2EEControlRecord.id.asc(), E2EEControlRecord.origin_domain.asc())
            .limit(limit + 1)
        )
    )
    controls = [
        rendered
        for record in candidates[:limit]
        if (rendered := e2ee_control_record_payload(record)) is not None
    ]
    next_after = None
    if len(candidates) > limit:
        cursor = candidates[limit - 1]
        next_after = f"{cursor.id}@{cursor.origin_domain}"
    return {
        "webhook_ref": webhook_e2ee_ref(webhook),
        "channel_ref": f"{channel.id}@{channel.origin_domain}",
        "device_id": device.protocol_id,
        "controls": controls,
        "next_after": next_after,
    }


async def revoke_webhook_e2ee_access(
    session: AsyncSession,
    settings: Settings,
    webhook: Webhook,
    actor: User,
) -> tuple[Guild | None, list[Channel]]:
    """Fence every automation device/room grant in the caller's transaction."""

    devices = list(
        await session.scalars(
            select(WebhookE2EEDevice)
            .where(
                WebhookE2EEDevice.webhook_id == webhook.id,
                WebhookE2EEDevice.webhook_domain == webhook.guild_domain,
                WebhookE2EEDevice.revoked_at.is_(None),
            )
            .order_by(WebhookE2EEDevice.id)
            .with_for_update()
        )
    )
    participations = list(
        await session.scalars(
            select(WebhookE2EEParticipation)
            .where(
                WebhookE2EEParticipation.webhook_id == webhook.id,
                WebhookE2EEParticipation.webhook_domain == webhook.guild_domain,
                WebhookE2EEParticipation.status.in_(("pending", "active")),
            )
            .order_by(WebhookE2EEParticipation.id)
            .with_for_update()
        )
    )
    refs = {(item.channel_id, item.channel_domain) for item in participations}
    channels = (
        list(
            await session.scalars(
                select(Channel)
                .where(
                    Channel.guild_id == webhook.guild_id,
                    Channel.guild_domain == webhook.guild_domain,
                    Channel.id.in_([item[0] for item in refs]),
                )
                .order_by(Channel.id)
                .with_for_update()
            )
        )
        if refs
        else []
    )
    now = datetime.now(UTC)
    for device in devices:
        device.trust_state = "revoked"
        device.revoked_at = now
        device.generation += 1
    for participation in participations:
        participation.status = "revoked"
        participation.revoked_at = now
        participation.consent_generation += 1
    guild = await session.get(Guild, (webhook.guild_id, webhook.guild_domain))
    if guild is None and channels:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    for channel in channels:
        if channel.encryption_mode == "e2ee" and channel.encryption_state == "active":
            channel.encryption_state = "rekeying"
            if guild is not None:
                await queue_guild_mutation(
                    session,
                    settings,
                    guild,
                    actor,
                    "guild.channel.update",
                    {"channel": federation_channel_state(channel)},
                    channel=channel,
                )
    return guild, channels


async def publish_webhook_e2ee_revocation(
    session: AsyncSession,
    redis: Redis,
    guild: Guild | None,
    channels: list[Channel],
) -> None:
    if guild is None or not channels:
        return
    await materialize_updated_at(session, *channels)
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    for channel in channels:
        if channel.encryption_state == "rekeying":
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "CHANNEL_UPDATE",
                channel_payload(channel),
            )
