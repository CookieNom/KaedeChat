from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bot_federation import refresh_user_bot_application
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.auth.instance_restrictions import require_remote_user_creation_allowed
from app.bots.auth import BotPrincipal, require_application_home_bot, require_bot
from app.bots.dm_capability import usable_dm_capability, validated_bot_dm_capability_proof
from app.bots.e2ee import (
    BOT_E2EE_CAPABILITIES,
    BOT_E2EE_DEVICE_SNAPSHOT_EVENT,
    MAX_BOT_E2EE_CREDENTIAL_BYTES,
    MAX_BOT_E2EE_DEVICE_SNAPSHOT_EVENT,
    MAX_BOT_E2EE_KEY_PACKAGE_BYTES,
    MAX_BOT_E2EE_KEY_PACKAGES_PER_DEVICE,
    BotE2EEDeviceSnapshot,
    bot_device_protocol_id,
    bot_device_registration_input,
    bot_key_package_upload_input,
    bot_mls_credential,
    local_bot_e2ee_snapshot,
    materialize_bot_e2ee_snapshot,
    render_bot_e2ee_device,
    require_bot_e2ee_participation,
    revoke_bot_e2ee_devices,
    validated_bot_e2ee_snapshot,
)
from app.bots.installations import usable_guild_installation, usable_user_installation
from app.bots.target_discovery import (
    queue_application_target_snapshots_for_refs,
    wake_application_target_deliveries,
)
from app.bots.user_install_authority import (
    USER_INSTALLATION_AUTHORITY_LEASE,
    FederatedUserInstallationGrant,
    federated_user_installation_lock,
    locked_federated_user_installation,
    reconcile_federated_user_installation,
    require_federated_user_application,
)
from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.e2ee import E2EE_SUITE_MLS_128, channel_encryption_policy_payload
from app.chat.e2ee_controls import e2ee_control_record_payload
from app.chat.e2ee_membership import (
    pause_local_e2ee_for_device_change,
    publish_e2ee_policy_updates,
)
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import (
    federation_channel_state,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import channel_payload
from app.chat.permissions import require_permissions
from app.chat.postcommit import publish_committed_dispatches
from app.chat.schemas import RequestModel
from app.core.base64url import decode_base64url, encode_base64url
from app.core.channel_types import is_message_capable_channel_type
from app.core.permissions import Permission
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.bot_models import (
    BotApplication,
    BotApplicationTarget,
    BotDMCapability,
    BotDMGrant,
    BotDMGrantConsent,
    BotE2EEDevice,
    BotE2EEKeyPackage,
    BotE2EEParticipation,
    BotInstallation,
    BotUserInstallation,
)
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Channel,
    DMConversation,
    DMParticipant,
    E2EEControlRecord,
    Guild,
    Message,
    User,
)
from app.federation.client import signed_request
from app.federation.events import (
    build_envelope,
    discard_superseded_latest_state_event,
    queue_event,
)
from app.federation.guild_management import (
    GuildManagementRequest,
    proxy_remote_guild_management,
)
from app.federation.management_rpc import (
    MANAGEMENT_RPC_DEADLINE_SECONDS,
    ManagementRPCErrorContract,
    consume_management_request_once,
    request_management_rpc,
    validate_management_json,
    validate_management_request_shape,
)
from app.federation.network import FederationNetworkError, decode_federation_response_json
from app.federation.replication import profile_from_user, upsert_remote_user
from app.federation.schemas import RemoteUserProfile
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    require_guild_federation_access,
)

router = APIRouter(tags=["bot end-to-end encryption"])
BOT_DEVICE_CHALLENGE_TTL_SECONDS = 300
BOT_KEY_PACKAGE_MAX_LIFETIME = timedelta(days=30)
BOT_E2EE_DEVICE_LIMIT = ClientRateLimit("bot-e2ee-device", 30, 60)
BOT_E2EE_PACKAGE_LIMIT = ClientRateLimit("bot-e2ee-package", 60, 60)
BOT_E2EE_AUDIT_ACTION_UPDATE_INTEGRATION = 81


class BotDeviceChallengeRequest(RequestModel):
    identity_key: str = Field(min_length=43, max_length=43)
    credential_digest: str = Field(min_length=43, max_length=43)

    @field_validator("identity_key", "credential_digest")
    @classmethod
    def canonical_32_bytes(cls, value: str) -> str:
        decode_base64url(value, size=32)
        return value


class BotDeviceRegisterRequest(RequestModel):
    challenge_id: str = Field(pattern=r"^kbec_[A-Za-z0-9_-]{32}$")
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
        decode_base64url(value, maximum=MAX_BOT_E2EE_CREDENTIAL_BYTES)
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
            or not set(value) <= BOT_E2EE_CAPABILITIES
            or "e2ee-mls/1" not in value
        ):
            raise ValueError("bot E2EE capabilities are invalid")
        return sorted(value)


class BotKeyPackageUploadRequest(RequestModel):
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
            decoded = decode_base64url(package, maximum=MAX_BOT_E2EE_KEY_PACKAGE_BYTES)
            if not decoded:
                raise ValueError("key packages cannot be empty")
        return value

    @field_validator("signature")
    @classmethod
    def canonical_signature(cls, value: str) -> str:
        decode_base64url(value, size=64)
        return value

    @model_validator(mode="after")
    def timezone_required(self) -> BotKeyPackageUploadRequest:
        if self.expires_at.tzinfo is None:
            raise ValueError("key package expiry requires a timezone")
        return self


class BotE2EESnapshotRequest(RequestModel):
    target_domain: str = Field(min_length=1, max_length=253)
    guild_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    guild_domain: str | None = Field(default=None, min_length=1, max_length=253)
    user_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    user_domain: str | None = Field(default=None, min_length=1, max_length=253)
    grant_id: str | None = Field(default=None, pattern=r"^kbdg_[A-Za-z0-9_-]{43}$")
    revision: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    conversation_ref: EntityRef | None = None
    channel_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    channel_domain: str = Field(min_length=1, max_length=253)

    @model_validator(mode="after")
    def one_installation_context(self) -> BotE2EESnapshotRequest:
        guild_context = self.guild_id is not None and self.guild_domain is not None
        user_context = self.user_id is not None and self.user_domain is not None
        capability_context = all(
            item is not None for item in (self.grant_id, self.revision, self.conversation_ref)
        )
        partial_context = any(
            item is not None
            for item in (
                self.guild_id,
                self.guild_domain,
                self.user_id,
                self.user_domain,
                self.grant_id,
                self.revision,
                self.conversation_ref,
            )
        )
        if not partial_context or sum((guild_context, user_context, capability_context)) != 1:
            raise ValueError("device snapshot requires exactly one complete authorization context")
        if any(item is not None for item in (self.guild_id, self.guild_domain)) != guild_context:
            raise ValueError("device snapshot guild context is incomplete")
        if any(item is not None for item in (self.user_id, self.user_domain)) != user_context:
            raise ValueError("device snapshot user context is incomplete")
        if (
            any(item is not None for item in (self.grant_id, self.revision, self.conversation_ref))
            != capability_context
        ):
            raise ValueError("device snapshot capability context is incomplete")
        if (
            capability_context
            and self.conversation_ref is not None
            and (
                self.conversation_ref.domain is None
                or self.conversation_ref.id != int(self.channel_id)
                or self.conversation_ref.domain != self.channel_domain
            )
        ):
            raise ValueError("device snapshot capability conversation is not canonical")
        return self


class BotE2EEManagementPayload(RequestModel):
    channel_ref: EntityRef
    application_ref: EntityRef | None = None
    reason: str | None = Field(default=None, max_length=512)


class BotDME2EEAuthorityRequest(RequestModel):
    request_id: str = Field(pattern=r"^kadme_[A-Za-z0-9_-]{32}$")
    issued_at: int = Field(ge=0)
    deadline: int = Field(ge=1)
    operation: Literal["get", "grant", "revoke"]
    channel_ref: EntityRef
    application_ref: EntityRef
    actor: RemoteUserProfile
    user_installation: FederatedUserInstallationGrant | None = None
    device_snapshot: dict[str, Any] | None = None

    @model_validator(mode="after")
    def bounded_authority_request(self) -> BotDME2EEAuthorityRequest:
        validate_management_request_shape(
            self.issued_at,
            self.deadline,
            label="dm-bot-e2ee",
        )
        has_installation = self.user_installation is not None
        has_snapshot = self.device_snapshot is not None
        if has_installation != has_snapshot or (self.operation != "grant" and has_installation):
            raise ValueError("DM bot E2EE grant proofs do not match the operation")
        validate_management_json(
            {
                "user_installation": (
                    None
                    if self.user_installation is None
                    else self.user_installation.model_dump(mode="json")
                ),
                "device_snapshot": self.device_snapshot,
            },
            label="dm-bot-e2ee payload",
        )
        if self.channel_ref.domain is None or self.application_ref.domain is None:
            raise ValueError("DM bot E2EE authority references must be qualified")
        return self


class BotDME2EEAuthorityResult(RequestModel):
    request_id: str = Field(pattern=r"^kadme_[A-Za-z0-9_-]{32}$")
    operation: Literal["get", "grant", "revoke"]
    channel_ref: EntityRef
    application_ref: EntityRef
    body: dict[str, Any]

    @model_validator(mode="after")
    def bounded_authority_result(self) -> BotDME2EEAuthorityResult:
        validate_management_json(self.body, label="dm-bot-e2ee response")
        if self.channel_ref.domain is None or self.application_ref.domain is None:
            raise ValueError("DM bot E2EE result references must be qualified")
        try:
            body_channel_ref = EntityRef(cast(str, self.body["channel_ref"]))
            body_application_ref = EntityRef(cast(str, self.body["application_ref"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError("DM bot E2EE result body identity is invalid") from None
        if (
            body_channel_ref.domain is None
            or body_application_ref.domain is None
            or body_channel_ref != self.channel_ref
            or body_application_ref != self.application_ref
        ):
            raise ValueError("DM bot E2EE result body identity does not match")
        return self


_DM_BOT_E2EE_RPC_ERRORS = ManagementRPCErrorContract(
    unavailable={"code": "E2EE_ROOM_AUTHORITY_UNREACHABLE"},
    failed={"code": "E2EE_ROOM_AUTHORITY_REJECTED"},
    invalid_response={"code": "E2EE_ROOM_AUTHORITY_INVALID_RESPONSE"},
    invalid_binding={"code": "E2EE_ROOM_AUTHORITY_INVALID_RESPONSE"},
)


def _challenge_key(challenge_id: str) -> str:
    return f"bot:e2ee:device-challenge:{challenge_id}"


def _principal_worker_authority_id(principal: BotPrincipal) -> int:
    return principal.worker.authority_id


async def _queue_bot_device_generation_change(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    bot: User,
    *,
    already_paused: Sequence[Channel] = (),
) -> tuple[list[Channel], set[str]]:
    """Queue one durable device snapshot per destination without committing.

    The caller owns the transaction so device trust, the generation bump,
    paused MLS rooms, and any application-runtime revocation projection cannot
    become visible independently.
    """

    newly_paused = await pause_local_e2ee_for_device_change(session, settings, bot)
    paused_channels = list(
        {
            (channel.id, channel.origin_domain): channel
            for channel in [*already_paused, *newly_paused]
        }.values()
    )
    destinations = set(
        await session.scalars(
            select(BotApplicationTarget.target_domain).where(
                BotApplicationTarget.application_id == application.id,
                BotApplicationTarget.application_domain == application.origin_domain,
                BotApplicationTarget.target_domain != settings.domain,
                (
                    (BotApplicationTarget.guild_installations > 0)
                    | (BotApplicationTarget.user_installations > 0)
                ),
            )
        )
    )
    destinations.update(
        await session.scalars(
            select(BotDMCapability.authority_domain).where(
                BotDMCapability.application_id == application.id,
                BotDMCapability.application_domain == application.origin_domain,
                BotDMCapability.bot_user_id == bot.id,
                BotDMCapability.bot_user_domain == bot.origin_domain,
                BotDMCapability.authority_domain != settings.domain,
                BotDMCapability.conversation_id.is_not(None),
                BotDMCapability.e2ee_mode == "participant",
                usable_dm_capability(at=datetime.now(UTC)),
            )
        )
    )
    for destination in sorted(destinations):
        await discard_superseded_latest_state_event(
            session,
            destination=destination,
            event_type="e2ee.device-list.changed",
            actor_ref=(bot.id, bot.origin_domain),
        )
        envelope = await build_envelope(
            session,
            settings,
            "e2ee.device-list.changed",
            bot,
            {"profile": profile_from_user(bot)},
        )
        await queue_event(session, settings, destination, envelope)
    return paused_channels, destinations


async def _publish_bot_device_generation_change(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    paused_channels: Sequence[Channel],
    destinations: set[str],
) -> None:
    """Publish only after the caller has committed the complete mutation."""

    await publish_committed_dispatches(session, redis)
    await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    if destinations:
        from app.tasks import federation_deliver

        for destination in destinations:
            await enqueue_best_effort(federation_deliver, destination)


async def _lock_local_bot(principal: BotPrincipal, session: AsyncSession) -> User:
    bot = await session.scalar(
        select(User)
        .where(
            User.id == principal.user.id,
            User.origin_domain == principal.user.origin_domain,
            User.is_local.is_(True),
            User.account_type == "bot",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if bot is None:
        raise HTTPException(status_code=409, detail={"code": "BOT_APPLICATION_HOME_REQUIRED"})
    return bot


@router.post("/api/v1/bots/e2ee/devices/challenge", status_code=201)
async def create_bot_e2ee_device_challenge(
    payload: BotDeviceChallengeRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await enforce_keyed_rate_limit(
        redis,
        response,
        BOT_E2EE_DEVICE_LIMIT,
        identity=(
            f"{principal.application.origin_domain}:{principal.application.id}:"
            f"{principal.worker.id}"
        ),
    )
    identity_key = decode_base64url(payload.identity_key, size=32)
    credential_digest = decode_base64url(payload.credential_digest, size=32)
    challenge_id = f"kbec_{secrets.token_urlsafe(24)}"
    challenge = secrets.token_bytes(32)
    signing_input = bot_device_registration_input(
        application_id=principal.application.id,
        application_domain=principal.application.origin_domain,
        worker_id=_principal_worker_authority_id(principal),
        identity_key=identity_key,
        credential_digest=credential_digest,
        challenge=challenge,
    )
    await redis.setex(
        _challenge_key(challenge_id),
        BOT_DEVICE_CHALLENGE_TTL_SECONDS,
        json.dumps(
            {
                "application_id": str(principal.application.id),
                "application_domain": principal.application.origin_domain,
                "worker_id": str(principal.worker.id),
                "worker_authority_id": str(_principal_worker_authority_id(principal)),
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
        "expires_in": BOT_DEVICE_CHALLENGE_TTL_SECONDS,
        "application_ref": (f"{principal.application.id}@{principal.application.origin_domain}"),
        "worker_id": str(_principal_worker_authority_id(principal)),
        "domain": settings.domain,
    }


@router.post("/api/v1/bots/e2ee/devices", status_code=201)
async def register_bot_e2ee_device(
    payload: BotDeviceRegisterRequest,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    raw_challenge = await redis.getdel(_challenge_key(payload.challenge_id))
    if not isinstance(raw_challenge, str):
        raise HTTPException(
            status_code=409,
            detail={"code": "BOT_E2EE_DEVICE_CHALLENGE_EXPIRED"},
        )
    try:
        challenge = json.loads(raw_challenge)
    except json.JSONDecodeError as exc:
        raise RuntimeError("stored bot E2EE challenge is corrupt") from exc
    identity_key = decode_base64url(payload.identity_key, size=32)
    credential = decode_base64url(
        payload.credential,
        maximum=MAX_BOT_E2EE_CREDENTIAL_BYTES,
    )
    expected = {
        "application_id": str(principal.application.id),
        "application_domain": principal.application.origin_domain,
        "worker_id": str(principal.worker.id),
        "worker_authority_id": str(_principal_worker_authority_id(principal)),
        "identity_key": payload.identity_key,
        "credential_digest": encode_base64url(hashlib.sha256(credential).digest()),
        "signing_input": challenge.get("signing_input"),
    }
    if challenge != expected:
        raise HTTPException(
            status_code=409,
            detail={"code": "BOT_E2EE_DEVICE_CHALLENGE_MISMATCH"},
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
            detail={"code": "BOT_E2EE_DEVICE_PROOF_INVALID"},
        ) from exc

    bot = await _lock_local_bot(principal, session)
    worker_authority_id = _principal_worker_authority_id(principal)
    protocol_id = bot_device_protocol_id(
        principal.application.id,
        principal.application.origin_domain,
        worker_authority_id,
        identity_key,
    )
    expected_credential = bot_mls_credential(
        principal.application.id,
        principal.application.origin_domain,
        worker_authority_id,
        protocol_id,
    )
    if not secrets.compare_digest(credential, expected_credential):
        raise HTTPException(
            status_code=400,
            detail={"code": "BOT_E2EE_DEVICE_CREDENTIAL_INVALID"},
        )
    existing = await session.scalar(
        select(BotE2EEDevice).where(
            BotE2EEDevice.application_id == principal.application.id,
            BotE2EEDevice.application_domain == principal.application.origin_domain,
            BotE2EEDevice.worker_id == principal.worker.id,
            BotE2EEDevice.revoked_at.is_(None),
        )
    )
    if existing is not None:
        if not secrets.compare_digest(
            existing.identity_key,
            identity_key,
        ) or not secrets.compare_digest(existing.credential, credential):
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_WORKER_DEVICE_EXISTS"},
            )
        if set(existing.capabilities or []) != set(payload.capabilities):
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_DEVICE_GENERATION_CONFLICT"},
            )
        worker = principal.worker
        return render_bot_e2ee_device(existing, worker)

    bot.e2ee_device_generation = max(0, int(bot.e2ee_device_generation or 0)) + 1
    device = BotE2EEDevice(
        id=await snowflake.mint(),
        protocol_id=protocol_id,
        application_id=principal.application.id,
        application_domain=principal.application.origin_domain,
        worker_id=principal.worker.id,
        identity_key=identity_key,
        credential=credential,
        capabilities=list(payload.capabilities),
        generation=bot.e2ee_device_generation,
        trust_state="trusted",
    )
    session.add(device)
    await session.flush()
    paused_channels, destinations = await _queue_bot_device_generation_change(
        session,
        settings,
        principal.application,
        bot,
    )
    await session.commit()
    await _publish_bot_device_generation_change(
        session,
        redis,
        settings,
        paused_channels,
        destinations,
    )
    return render_bot_e2ee_device(device, principal.worker)


@router.get("/api/v1/bots/e2ee/devices")
async def list_bot_e2ee_devices(
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    snapshot = await local_bot_e2ee_snapshot(session, principal.application, principal.user)
    now = datetime.now(UTC)
    counts = {
        int(device_id): int(count)
        for device_id, count in (
            await session.execute(
                select(BotE2EEKeyPackage.device_id, func.count(BotE2EEKeyPackage.id))
                .where(
                    BotE2EEKeyPackage.claimed_at.is_(None),
                    BotE2EEKeyPackage.expires_at > now,
                )
                .group_by(BotE2EEKeyPackage.device_id)
            )
        ).tuples()
    }
    source_to_local = {
        int(device.source_id if device.source_id is not None else device.id): device.id
        for device in await session.scalars(
            select(BotE2EEDevice).where(
                BotE2EEDevice.application_id == principal.application.id,
                BotE2EEDevice.application_domain == principal.application.origin_domain,
            )
        )
    }
    return {
        "generation": snapshot.generation,
        "devices": [
            descriptor.model_dump(mode="json")
            | {
                "available_key_packages": counts.get(
                    source_to_local.get(int(descriptor.source_id), -1),
                    0,
                )
            }
            for descriptor in snapshot.devices
        ],
    }


async def _current_worker_device(
    session: AsyncSession,
    principal: BotPrincipal,
    protocol_id: str,
    *,
    for_update: bool = False,
) -> BotE2EEDevice:
    query = select(BotE2EEDevice).where(
        BotE2EEDevice.protocol_id == protocol_id,
        BotE2EEDevice.application_id == principal.application.id,
        BotE2EEDevice.application_domain == principal.application.origin_domain,
        BotE2EEDevice.worker_id == principal.worker.id,
        BotE2EEDevice.trust_state == "trusted",
        BotE2EEDevice.revoked_at.is_(None),
    )
    if for_update:
        query = query.with_for_update()
    device = await session.scalar(query)
    if device is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_E2EE_DEVICE_NOT_FOUND"})
    return device


@router.post("/api/v1/bots/e2ee/devices/{protocol_id}/key-packages", status_code=201)
async def upload_bot_e2ee_key_packages(
    protocol_id: str,
    payload: BotKeyPackageUploadRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
) -> dict[str, object]:
    await enforce_keyed_rate_limit(
        redis,
        response,
        BOT_E2EE_PACKAGE_LIMIT,
        identity=f"{principal.application.origin_domain}:{principal.application.id}:{protocol_id}",
    )
    now = datetime.now(UTC)
    expires_at = payload.expires_at.astimezone(UTC)
    if expires_at <= now + timedelta(minutes=5) or expires_at > now + BOT_KEY_PACKAGE_MAX_LIFETIME:
        raise HTTPException(
            status_code=400,
            detail={"code": "BOT_E2EE_KEY_PACKAGE_EXPIRY_INVALID"},
        )
    device = await _current_worker_device(
        session,
        principal,
        protocol_id,
        for_update=True,
    )
    decoded = [
        decode_base64url(item, maximum=MAX_BOT_E2EE_KEY_PACKAGE_BYTES) for item in payload.packages
    ]
    package_hashes = [hashlib.sha256(item).digest() for item in decoded]
    signing_input = bot_key_package_upload_input(
        protocol_id=device.protocol_id,
        generation=device.generation,
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
            detail={"code": "BOT_E2EE_KEY_PACKAGE_PROOF_INVALID"},
        ) from exc
    existing = list(
        await session.scalars(
            select(BotE2EEKeyPackage).where(
                BotE2EEKeyPackage.device_id == device.id,
                BotE2EEKeyPackage.package_hash.in_(package_hashes),
            )
        )
    )
    if any(package.claimed_at is not None for package in existing):
        raise HTTPException(
            status_code=409,
            detail={"code": "BOT_E2EE_KEY_PACKAGE_REUSE"},
        )
    available = int(
        await session.scalar(
            select(func.count(BotE2EEKeyPackage.id)).where(
                BotE2EEKeyPackage.device_id == device.id,
                BotE2EEKeyPackage.claimed_at.is_(None),
                BotE2EEKeyPackage.expires_at > now,
            )
        )
        or 0
    )
    existing_hashes = {package.package_hash for package in existing}
    new_packages = [
        (package, package_hash)
        for package, package_hash in zip(decoded, package_hashes, strict=True)
        if package_hash not in existing_hashes
    ]
    if available + len(new_packages) > MAX_BOT_E2EE_KEY_PACKAGES_PER_DEVICE:
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_KEY_PACKAGE_LIMIT"})
    for package, package_hash in new_packages:
        session.add(
            BotE2EEKeyPackage(
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


@router.delete(
    "/api/v1/bots/e2ee/devices/{protocol_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_bot_e2ee_device(
    protocol_id: str,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await _lock_local_bot(principal, session)
    device = await _current_worker_device(
        session,
        principal,
        protocol_id,
        for_update=True,
    )
    now = datetime.now(UTC)
    bot, paused_channels = await revoke_bot_e2ee_devices(
        session,
        redis,
        settings,
        application_ref=(principal.application.id, principal.application.origin_domain),
        device_ids=(device.id,),
        now=now,
    )
    if bot is None:
        raise RuntimeError("locked bot E2EE device was not revoked")
    paused_channels, destinations = await _queue_bot_device_generation_change(
        session,
        settings,
        principal.application,
        bot,
        already_paused=paused_channels,
    )
    await session.commit()
    await _publish_bot_device_generation_change(
        session,
        redis,
        settings,
        paused_channels,
        destinations,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/_kaede/v1/bot-e2ee/applications/{application_id}/devices")
async def federation_bot_e2ee_device_snapshot(
    application_id: int,
    payload: BotE2EESnapshotRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild_context = payload.guild_id is not None
    if guild_context:
        require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-e2ee-device-snapshot",
        capacity=240,
        refill_per_minute=240,
    )
    capability_context = payload.grant_id is not None
    context_matches = (
        payload.guild_domain == principal.origin and payload.channel_domain == principal.origin
    ) or payload.user_domain == principal.origin
    if capability_context:
        context_matches = (
            payload.channel_domain == principal.origin
            and payload.conversation_ref is not None
            and payload.conversation_ref.domain == principal.origin
        )
    if payload.target_domain != principal.origin or not context_matches:
        raise HTTPException(status_code=404, detail={"code": "BOT_E2EE_APPLICATION_NOT_FOUND"})
    row = (
        await session.execute(
            select(BotApplication, User)
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplication.id == application_id,
                BotApplication.origin_domain == settings.domain,
                BotApplication.status == "active",
                User.account_type == "bot",
                User.is_local.is_(True),
                User.disabled_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_E2EE_APPLICATION_NOT_FOUND"})
    application, bot = row
    if capability_context:
        capability = await session.scalar(
            select(BotDMCapability).where(
                BotDMCapability.grant_id == payload.grant_id,
                BotDMCapability.revision == int(cast(str, payload.revision)),
                BotDMCapability.application_id == application.id,
                BotDMCapability.application_domain == application.origin_domain,
                BotDMCapability.bot_user_id == bot.id,
                BotDMCapability.bot_user_domain == bot.origin_domain,
                BotDMCapability.authority_domain == principal.origin,
                BotDMCapability.conversation_id == int(payload.channel_id),
                BotDMCapability.conversation_domain == payload.channel_domain,
                BotDMCapability.e2ee_mode == "participant",
                usable_dm_capability(at=datetime.now(UTC)),
            )
        )
        if capability is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "BOT_E2EE_APPLICATION_NOT_FOUND"},
            )
        try:
            _, capability_proof = await validated_bot_dm_capability_proof(
                session,
                settings,
                capability.proof,
                expected_installation_authority=capability.source_installation_domain,
            )
        except ValueError:
            raise HTTPException(
                status_code=404,
                detail={"code": "BOT_E2EE_APPLICATION_NOT_FOUND"},
            ) from None
        if (
            capability_proof.grant_id != capability.grant_id
            or int(capability_proof.revision) != capability.revision
            or capability_proof.application.id != application.id
            or capability_proof.application.domain != application.origin_domain
            or capability_proof.bot_user.id != bot.id
            or capability_proof.bot_user.domain != bot.origin_domain
            or capability_proof.authority_domain != principal.origin
            or capability_proof.e2ee_mode != "participant"
            or capability_proof.status != "active"
        ):
            raise HTTPException(
                status_code=404,
                detail={"code": "BOT_E2EE_APPLICATION_NOT_FOUND"},
            )
    else:
        target = await session.get(
            BotApplicationTarget,
            (application_id, settings.domain, principal.origin),
        )
        installation_count = 0
        if target is not None:
            installation_count = (
                target.guild_installations if guild_context else target.user_installations
            )
        if not installation_count:
            raise HTTPException(
                status_code=404,
                detail={"code": "BOT_E2EE_APPLICATION_NOT_FOUND"},
            )
    snapshot = await local_bot_e2ee_snapshot(session, application, bot)
    return await build_envelope(
        session,
        settings,
        BOT_E2EE_DEVICE_SNAPSHOT_EVENT,
        bot,
        snapshot.model_dump(mode="json"),
    )


async def _fetch_remote_device_snapshot_envelope(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    bot: User,
    channel: Channel,
    installation_context: dict[str, object],
) -> tuple[dict[str, object], BotE2EEDeviceSnapshot]:
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            application.origin_domain,
            f"/_kaede/v1/bot-e2ee/applications/{application.id}/devices",
            payload={
                "target_domain": settings.domain,
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                **installation_context,
            },
            request_timeout=10,
            max_response_bytes=MAX_BOT_E2EE_DEVICE_SNAPSHOT_EVENT,
            guild_context=getattr(channel, "guild_id", None) is not None,
        )
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "BOT_E2EE_APPLICATION_HOME_UNREACHABLE"},
        ) from exc
    raw = decode_federation_response_json(response)
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=502,
            detail={"code": "BOT_E2EE_DEVICE_SNAPSHOT_INVALID"},
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=409 if response.status_code == 409 else 502,
            detail={"code": "BOT_E2EE_DEVICE_SNAPSHOT_REJECTED"},
        )
    try:
        snapshot = await validated_bot_e2ee_snapshot(
            session,
            settings,
            application.origin_domain,
            raw,
            application_id=application.id,
            bot_user_ref=(bot.id, bot.origin_domain),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "BOT_E2EE_DEVICE_SNAPSHOT_INVALID"},
        ) from exc
    return {str(key): value for key, value in raw.items()}, snapshot


async def _fetch_remote_device_snapshot(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    bot: User,
    channel: Channel,
    installation_context: dict[str, object],
) -> BotE2EEDeviceSnapshot:
    _, snapshot = await _fetch_remote_device_snapshot_envelope(
        session,
        settings,
        application,
        bot,
        channel,
        installation_context,
    )
    return snapshot


async def _remote_device_snapshot(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    bot: User,
    guild: Guild,
    channel: Channel,
) -> BotE2EEDeviceSnapshot:
    return await _fetch_remote_device_snapshot(
        session,
        settings,
        application,
        bot,
        channel,
        {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
        },
    )


async def _bot_installation_for_e2ee(
    session: AsyncSession,
    guild: Guild,
    application_ref: EntityRef,
    settings: Settings,
) -> tuple[BotApplication, User, BotInstallation]:
    application_id, application_domain = application_ref.resolve(settings.domain)
    row = (
        await session.execute(
            select(BotApplication, User, BotInstallation)
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .join(
                BotInstallation,
                (BotInstallation.application_id == BotApplication.id)
                & (BotInstallation.application_domain == BotApplication.origin_domain),
            )
            .where(
                BotApplication.id == application_id,
                BotApplication.origin_domain == application_domain,
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
                BotInstallation.guild_id == guild.id,
                BotInstallation.guild_domain == guild.origin_domain,
                usable_guild_installation(),
                BotInstallation.e2ee_mode == "participant",
            )
            .with_for_update(of=BotInstallation)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "BOT_E2EE_PARTICIPANT_INSTALLATION_NOT_FOUND"},
        )
    return cast(tuple[BotApplication, User, BotInstallation], row)


async def _authorize_bot_e2ee_management(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    actor: User,
    channel_ref: EntityRef,
) -> Channel:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel = await session.get(Channel, (channel_id, channel_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.origin_domain != settings.domain
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        or not is_message_capable_channel_type(channel.type, guild_channel=True)
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    needed = (
        Permission.MANAGE_THREADS if channel.type in {10, 11, 12} else Permission.MANAGE_CHANNELS
    )
    await require_permissions(session, redis, guild, actor, needed, channel=channel)
    return channel


async def _device_snapshot_for_installation(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    channel: Channel,
    application: BotApplication,
    bot: User,
) -> tuple[BotE2EEDeviceSnapshot, list[BotE2EEDevice]]:
    if application.origin_domain == settings.domain:
        snapshot = await local_bot_e2ee_snapshot(session, application, bot)
        devices = list(
            await session.scalars(
                select(BotE2EEDevice).where(
                    BotE2EEDevice.application_id == application.id,
                    BotE2EEDevice.application_domain == application.origin_domain,
                    BotE2EEDevice.protocol_id.in_(
                        [descriptor.protocol_id for descriptor in snapshot.devices]
                    ),
                    BotE2EEDevice.trust_state == "trusted",
                    BotE2EEDevice.revoked_at.is_(None),
                )
            )
        )
    else:
        snapshot = await _remote_device_snapshot(
            session,
            settings,
            application,
            bot,
            guild,
            channel,
        )
        current_generation = max(0, int(bot.e2ee_device_generation or 0))
        incoming_generation = int(snapshot.generation)
        if incoming_generation < current_generation:
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_DEVICE_GENERATION_ROLLBACK"},
            )
        devices = await materialize_bot_e2ee_snapshot(
            session,
            snowflake,
            application,
            snapshot,
            known_generation=current_generation,
        )
        bot.e2ee_device_generation = incoming_generation
    if not devices:
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_DEVICE_REQUIRED"})
    return snapshot, devices


async def _dm_e2ee_context(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    channel_ref: EntityRef,
) -> tuple[DMConversation, Channel, list[User]]:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel = await session.get(Channel, (channel_id, channel_domain))
    conversation = await session.get(DMConversation, (channel_id, channel_domain))
    membership = await session.get(
        DMParticipant,
        (channel_id, channel_domain, actor.id, actor.origin_domain),
    )
    if (
        channel is None
        or channel.unavailable
        or channel.guild_id is not None
        or conversation is None
        or conversation.authority_domain != settings.domain
        or membership is None
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    participants = list(
        await session.scalars(
            select(User)
            .join(
                DMParticipant,
                (DMParticipant.user_id == User.id)
                & (DMParticipant.user_domain == User.origin_domain),
            )
            .where(
                DMParticipant.conversation_id == channel.id,
                DMParticipant.conversation_domain == channel.origin_domain,
                User.account_type != "bot",
                User.disabled_at.is_(None),
            )
            .order_by(User.origin_domain, User.id)
        )
    )
    return conversation, channel, participants


async def _dm_user_installation_for_e2ee(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    application_ref: EntityRef,
) -> tuple[BotApplication, User, BotUserInstallation]:
    application_id, application_domain = application_ref.resolve(settings.domain)
    row = (
        await session.execute(
            select(BotApplication, User, BotUserInstallation)
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .join(
                BotUserInstallation,
                (BotUserInstallation.application_id == BotApplication.id)
                & (BotUserInstallation.application_domain == BotApplication.origin_domain),
            )
            .where(
                BotApplication.id == application_id,
                BotApplication.origin_domain == application_domain,
                BotApplication.status == "active",
                BotApplication.e2ee_modes.contains(["participant"]),
                User.account_type == "bot",
                User.disabled_at.is_(None),
                BotUserInstallation.user_id == actor.id,
                BotUserInstallation.user_domain == actor.origin_domain,
                usable_user_installation(current_instance_domain=settings.domain),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "BOT_E2EE_PARTICIPANT_INSTALLATION_NOT_FOUND"},
        )
    application, bot, installation = row
    if not set(installation.contexts).intersection({"bot_dm", "private_channel"}):
        raise HTTPException(
            status_code=404,
            detail={"code": "BOT_E2EE_PARTICIPANT_INSTALLATION_NOT_FOUND"},
        )
    return application, bot, installation


async def _device_snapshot_for_user_installation(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    channel: Channel,
    actor: User,
    application: BotApplication,
    bot: User,
    capability: BotDMCapability | None = None,
) -> list[BotE2EEDevice]:
    if application.origin_domain == settings.domain:
        snapshot = await local_bot_e2ee_snapshot(session, application, bot)
        devices = list(
            await session.scalars(
                select(BotE2EEDevice).where(
                    BotE2EEDevice.application_id == application.id,
                    BotE2EEDevice.application_domain == application.origin_domain,
                    BotE2EEDevice.protocol_id.in_(
                        [descriptor.protocol_id for descriptor in snapshot.devices]
                    ),
                    BotE2EEDevice.trust_state == "trusted",
                    BotE2EEDevice.revoked_at.is_(None),
                )
            )
        )
    else:
        if capability is not None:
            if (
                capability.authority_domain != settings.domain
                or capability.conversation_id != channel.id
                or capability.conversation_domain != channel.origin_domain
                or capability.application_id != application.id
                or capability.application_domain != application.origin_domain
                or capability.bot_user_id != bot.id
                or capability.bot_user_domain != bot.origin_domain
                or capability.status != "active"
                or capability.revoked_at is not None
                or capability.expires_at <= datetime.now(UTC)
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "BOT_E2EE_PARTICIPANT_INSTALLATION_NOT_FOUND"},
                )
            installation_context: dict[str, object] = {
                "grant_id": capability.grant_id,
                "revision": str(capability.revision),
                "conversation_ref": f"{channel.id}@{channel.origin_domain}",
            }
        else:
            installation_context = {
                "user_id": str(actor.id),
                "user_domain": actor.origin_domain,
            }
        snapshot = await _fetch_remote_device_snapshot(
            session,
            settings,
            application,
            bot,
            channel,
            installation_context,
        )
        current_generation = max(0, int(bot.e2ee_device_generation or 0))
        if int(snapshot.generation) < current_generation:
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_DEVICE_GENERATION_ROLLBACK"},
            )
        devices = await materialize_bot_e2ee_snapshot(
            session,
            snowflake,
            application,
            snapshot,
            known_generation=current_generation,
        )
        bot.e2ee_device_generation = int(snapshot.generation)
    if not devices:
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_DEVICE_REQUIRED"})
    return devices


def _render_dm_bot_grant(
    grant: BotDMGrant,
    channel: Channel,
    participants: Sequence[User],
    consents: Sequence[BotDMGrantConsent],
    rows: Sequence[tuple[BotE2EEParticipation, BotE2EEDevice]],
) -> dict[str, object]:
    consent_by_ref = {(item.user_id, item.user_domain): item for item in consents}
    return {
        "application_ref": f"{grant.application_id}@{grant.application_domain}",
        "channel_ref": f"{channel.id}@{channel.origin_domain}",
        "consent_state": grant.consent_state,
        "consent_generation": str(grant.consent_generation),
        "history_floor_message_ref": (
            f"{grant.history_floor_message_id}@{grant.history_floor_message_domain}"
            if grant.history_floor_message_id is not None
            else None
        ),
        "participants": [
            {
                "user_ref": f"{participant.id}@{participant.origin_domain}",
                "consented": bool(
                    (consent := consent_by_ref.get((participant.id, participant.origin_domain)))
                    is not None
                    and consent.status == "active"
                    and consent.revoked_at is None
                    and consent.consent_generation == grant.consent_generation
                ),
            }
            for participant in participants
        ],
        "devices": [
            {
                "device_id": device.protocol_id,
                "status": participation.status,
                "joined_epoch": str(participation.joined_epoch),
            }
            for participation, device in rows
        ],
        "encryption_policy": channel_encryption_policy_payload(channel),
    }


async def _dm_grant_state(
    session: AsyncSession,
    grant: BotDMGrant,
    channel: Channel,
    participants: Sequence[User],
) -> dict[str, object]:
    consents = list(
        await session.scalars(
            select(BotDMGrantConsent)
            .where(BotDMGrantConsent.grant_id == grant.id)
            .order_by(BotDMGrantConsent.user_domain, BotDMGrantConsent.user_id)
        )
    )
    rows = list(
        (
            await session.execute(
                select(BotE2EEParticipation, BotE2EEDevice)
                .join(BotE2EEDevice, BotE2EEDevice.id == BotE2EEParticipation.device_id)
                .where(BotE2EEParticipation.dm_grant_id == grant.id)
                .order_by(BotE2EEDevice.protocol_id)
            )
        ).tuples()
    )
    return _render_dm_bot_grant(grant, channel, participants, consents, rows)


async def _pause_dm_bot_participation(
    session: AsyncSession,
    channel: Channel,
) -> None:
    if channel.encryption_mode == "e2ee" and channel.encryption_state == "active":
        channel.encryption_state = "rekeying"
    await session.flush()


async def _reconcile_bot_e2ee_participations(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    *,
    existing: Sequence[BotE2EEParticipation],
    devices: Sequence[BotE2EEDevice],
    channel: Channel,
    actor: User,
    owner_field: Literal["installation_id", "dm_grant_id"],
    owner_id: int,
    consent_generation: int | None = None,
) -> None:
    """Reconcile one channel's device rows for guild and DM bot grants."""

    accepted_ids = {device.id for device in devices}
    by_device = {participation.device_id: participation for participation in existing}
    now = datetime.now(UTC)
    for participation in existing:
        if participation.device_id not in accepted_ids and participation.status != "revoked":
            participation.status = "revoked"
            participation.revoked_at = now
            participation.consent_generation += 1
    for device in devices:
        current = by_device.get(device.id)
        if current is None:
            session.add(
                BotE2EEParticipation(
                    id=await snowflake.mint(),
                    **{owner_field: owner_id},
                    application_id=device.application_id,
                    application_domain=device.application_domain,
                    guild_id=channel.guild_id,
                    guild_domain=channel.guild_domain,
                    channel_id=channel.id,
                    channel_domain=channel.origin_domain,
                    device_id=device.id,
                    consenting_actor_id=actor.id,
                    consenting_actor_domain=actor.origin_domain,
                    consent_generation=consent_generation or 1,
                    joined_epoch=0,
                    history_floor_message_id=channel.last_message_id,
                    history_floor_message_domain=channel.last_message_domain,
                    status="pending",
                )
            )
        elif current.status == "revoked":
            current.status = "pending"
            current.revoked_at = None
            current.consent_generation = (
                consent_generation
                if consent_generation is not None
                else current.consent_generation + 1
            )
            current.joined_epoch = 0
            current.history_floor_message_id = channel.last_message_id
            current.history_floor_message_domain = channel.last_message_domain
            current.consenting_actor_id = actor.id
            current.consenting_actor_domain = actor.origin_domain


async def _grant_dm_bot_e2ee(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    actor: User,
    channel_ref: EntityRef,
    application_ref: EntityRef,
    prepared: tuple[BotApplication, User, BotUserInstallation, list[BotE2EEDevice]] | None = None,
) -> dict[str, object]:
    _, channel, participants = await _dm_e2ee_context(
        session,
        settings,
        actor,
        channel_ref,
    )
    application_id, application_domain = application_ref.resolve(settings.domain)
    grant = await session.scalar(
        select(BotDMGrant)
        .where(
            BotDMGrant.conversation_id == channel.id,
            BotDMGrant.conversation_domain == channel.origin_domain,
            BotDMGrant.application_id == application_id,
            BotDMGrant.application_domain == application_domain,
        )
        .with_for_update()
    )
    runtime_installation: BotUserInstallation | BotDMCapability
    if prepared is None:
        capability_query = select(BotDMCapability).where(
            BotDMCapability.application_id == application_id,
            BotDMCapability.application_domain == application_domain,
            BotDMCapability.target_user_id == actor.id,
            BotDMCapability.target_user_domain == actor.origin_domain,
            BotDMCapability.conversation_id == channel.id,
            BotDMCapability.conversation_domain == channel.origin_domain,
            BotDMCapability.e2ee_mode == "participant",
            usable_dm_capability(at=datetime.now(UTC)),
        )
        if grant is not None and grant.dm_capability_id is not None:
            capability_query = capability_query.where(BotDMCapability.id == grant.dm_capability_id)
        capability = await session.scalar(
            capability_query.order_by(BotDMCapability.grant_id).limit(1).with_for_update()
        )
        if capability is not None:
            application = await session.get(
                BotApplication,
                (capability.application_id, capability.application_domain),
            )
            bot = await session.get(
                User,
                (capability.bot_user_id, capability.bot_user_domain),
            )
            if (
                application is None
                or application.status != "active"
                or "participant" not in application.e2ee_modes
                or bot is None
                or bot.account_type != "bot"
                or bot.disabled_at is not None
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "BOT_E2EE_PARTICIPANT_INSTALLATION_NOT_FOUND"},
                )
            devices = await _device_snapshot_for_user_installation(
                session,
                snowflake,
                settings,
                channel,
                actor,
                application,
                bot,
                capability,
            )
            runtime_installation = capability
        else:
            application, bot, user_installation = await _dm_user_installation_for_e2ee(
                session,
                settings,
                actor,
                application_ref,
            )
            devices = await _device_snapshot_for_user_installation(
                session,
                snowflake,
                settings,
                channel,
                actor,
                application,
                bot,
            )
            runtime_installation = user_installation
    else:
        application, bot, user_installation, devices = prepared
        if (application.id, application.origin_domain) != application_ref.resolve(settings.domain):
            raise HTTPException(status_code=422, detail={"code": "APPLICATION_REF_REQUIRED"})
        runtime_installation = user_installation
    now = datetime.now(UTC)
    if grant is None:
        grant = BotDMGrant(
            id=await snowflake.mint(),
            conversation_id=channel.id,
            conversation_domain=channel.origin_domain,
            application_id=application.id,
            application_domain=application.origin_domain,
            user_installation_id=(
                runtime_installation.id
                if isinstance(runtime_installation, BotUserInstallation)
                else None
            ),
            dm_capability_id=(
                runtime_installation.id
                if isinstance(runtime_installation, BotDMCapability)
                else None
            ),
            granted_by_id=actor.id,
            granted_by_domain=actor.origin_domain,
            consent_state="pending",
            scopes=list(runtime_installation.granted_scopes),
            consent_generation=1,
        )
        session.add(grant)
        await session.flush()
    elif grant.consent_state == "revoked":
        grant.consent_state = "pending"
        grant.revoked_at = None
        grant.consent_generation += 1
        grant.user_installation_id = (
            runtime_installation.id
            if isinstance(runtime_installation, BotUserInstallation)
            else None
        )
        grant.dm_capability_id = (
            runtime_installation.id if isinstance(runtime_installation, BotDMCapability) else None
        )
        grant.installation_id = None
        grant.granted_by_id = actor.id
        grant.granted_by_domain = actor.origin_domain
        grant.scopes = list(runtime_installation.granted_scopes)
    consent = await session.get(
        BotDMGrantConsent,
        (grant.id, actor.id, actor.origin_domain),
    )
    if consent is None:
        session.add(
            BotDMGrantConsent(
                grant_id=grant.id,
                user_id=actor.id,
                user_domain=actor.origin_domain,
                consent_generation=grant.consent_generation,
                status="active",
            )
        )
    else:
        consent.consent_generation = grant.consent_generation
        consent.status = "active"
        consent.consented_at = now
        consent.revoked_at = None
    await session.flush()
    active_consents = set(
        (
            await session.execute(
                select(BotDMGrantConsent.user_id, BotDMGrantConsent.user_domain).where(
                    BotDMGrantConsent.grant_id == grant.id,
                    BotDMGrantConsent.consent_generation == grant.consent_generation,
                    BotDMGrantConsent.status == "active",
                    BotDMGrantConsent.revoked_at.is_(None),
                )
            )
        ).tuples()
    )
    participant_refs = {(item.id, item.origin_domain) for item in participants}
    if participant_refs <= active_consents:
        grant.consent_state = "active"
        grant.history_floor_message_id = channel.last_message_id
        grant.history_floor_message_domain = channel.last_message_domain
        existing = list(
            await session.scalars(
                select(BotE2EEParticipation)
                .where(BotE2EEParticipation.dm_grant_id == grant.id)
                .with_for_update()
            )
        )
        await _reconcile_bot_e2ee_participations(
            session,
            snowflake,
            existing=existing,
            devices=devices,
            channel=channel,
            actor=actor,
            owner_field="dm_grant_id",
            owner_id=grant.id,
            consent_generation=grant.consent_generation,
        )
        await _pause_dm_bot_participation(session, channel)
    await session.commit()
    await publish_e2ee_policy_updates(session, redis, settings, [channel])
    return await _dm_grant_state(session, grant, channel, participants)


async def _revoke_dm_bot_e2ee(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    channel_ref: EntityRef,
    application_ref: EntityRef,
) -> dict[str, object]:
    _, channel, participants = await _dm_e2ee_context(
        session,
        settings,
        actor,
        channel_ref,
    )
    application_id, application_domain = application_ref.resolve(settings.domain)
    grant = await session.scalar(
        select(BotDMGrant)
        .where(
            BotDMGrant.conversation_id == channel.id,
            BotDMGrant.conversation_domain == channel.origin_domain,
            BotDMGrant.application_id == application_id,
            BotDMGrant.application_domain == application_domain,
            BotDMGrant.consent_state != "revoked",
        )
        .with_for_update()
    )
    if grant is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_E2EE_PARTICIPATION_NOT_FOUND"})
    now = datetime.now(UTC)
    grant.consent_state = "revoked"
    grant.revoked_at = now
    grant.consent_generation += 1
    consent = await session.get(
        BotDMGrantConsent,
        (grant.id, actor.id, actor.origin_domain),
    )
    if consent is not None:
        consent.status = "revoked"
        consent.revoked_at = now
    rows = list(
        await session.scalars(
            select(BotE2EEParticipation)
            .where(
                BotE2EEParticipation.dm_grant_id == grant.id,
                BotE2EEParticipation.status.in_(("pending", "active")),
            )
            .with_for_update()
        )
    )
    for participation in rows:
        participation.status = "revoked"
        participation.revoked_at = now
        participation.consent_generation += 1
    await _pause_dm_bot_participation(session, channel)
    await session.commit()
    await publish_e2ee_policy_updates(session, redis, settings, [channel])
    return await _dm_grant_state(session, grant, channel, participants)


async def _get_dm_bot_e2ee(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    channel_ref: EntityRef,
    application_ref: EntityRef,
) -> dict[str, object]:
    _, channel, participants = await _dm_e2ee_context(
        session,
        settings,
        actor,
        channel_ref,
    )
    application_id, application_domain = application_ref.resolve(settings.domain)
    grant = await session.scalar(
        select(BotDMGrant).where(
            BotDMGrant.conversation_id == channel.id,
            BotDMGrant.conversation_domain == channel.origin_domain,
            BotDMGrant.application_id == application_id,
            BotDMGrant.application_domain == application_domain,
        )
    )
    if grant is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_E2EE_PARTICIPATION_NOT_FOUND"})
    return await _dm_grant_state(session, grant, channel, participants)


def _user_installation_assertion(
    installation: BotUserInstallation,
    *,
    authority_expires_at: datetime,
) -> dict[str, object]:
    return {
        "id": str(installation.source_id or installation.id),
        "application_ref": (f"{installation.application_id}@{installation.application_domain}"),
        "scopes": list(installation.granted_scopes),
        "intents": list(installation.granted_intents),
        "contexts": list(installation.contexts),
        "grant_revision": str(installation.grant_revision),
        "authority_expires_at": authority_expires_at.astimezone(UTC).isoformat(),
    }


async def _dm_device_snapshot_envelope(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
    actor: User,
    application: BotApplication,
    bot: User,
) -> dict[str, object]:
    if application.origin_domain == settings.domain:
        snapshot = await local_bot_e2ee_snapshot(session, application, bot)
        return await build_envelope(
            session,
            settings,
            BOT_E2EE_DEVICE_SNAPSHOT_EVENT,
            bot,
            snapshot.model_dump(mode="json"),
        )
    envelope, _ = await _fetch_remote_device_snapshot_envelope(
        session,
        settings,
        application,
        bot,
        channel,
        {"user_id": str(actor.id), "user_domain": actor.origin_domain},
    )
    return envelope


async def _dm_remote_authority(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    channel_ref: EntityRef,
) -> tuple[Channel, DMConversation] | None:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel = await session.get(Channel, (channel_id, channel_domain))
    conversation = await session.get(DMConversation, (channel_id, channel_domain))
    membership = await session.get(
        DMParticipant,
        (channel_id, channel_domain, actor.id, actor.origin_domain),
    )
    if channel is None or conversation is None or membership is None or channel.unavailable:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if conversation.authority_domain == settings.domain:
        return None
    return channel, conversation


async def _proxy_dm_bot_e2ee(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    channel_ref: EntityRef,
    application_ref: EntityRef,
    operation: Literal["get", "grant", "revoke"],
) -> dict[str, object] | None:
    remote = await _dm_remote_authority(session, settings, actor, channel_ref)
    if remote is None:
        return None
    channel, conversation = remote
    user_installation: dict[str, object] | None = None
    device_snapshot: dict[str, object] | None = None
    issued_at = int(time.time())
    if operation == "grant":
        try:
            application, bot, installation = await _dm_user_installation_for_e2ee(
                session,
                settings,
                actor,
                application_ref,
            )
        except HTTPException as exc:
            detail = cast(Any, exc.detail)
            if (
                exc.status_code != 404
                or not isinstance(detail, dict)
                or detail.get("code") != "BOT_E2EE_PARTICIPANT_INSTALLATION_NOT_FOUND"
            ):
                raise
        else:
            user_installation = _user_installation_assertion(
                installation,
                authority_expires_at=(
                    datetime.fromtimestamp(issued_at, UTC) + USER_INSTALLATION_AUTHORITY_LEASE
                ),
            )
            device_snapshot = await _dm_device_snapshot_envelope(
                session,
                settings,
                channel,
                actor,
                application,
                bot,
            )
    # A capability-authorized DM has no user-home install to assert. In that
    # case the conversation authority reuses its exact signed capability and
    # independently fetches the app-home device snapshot.
    application_id, application_domain = application_ref.resolve(settings.domain)
    request = BotDME2EEAuthorityRequest(
        request_id=f"kadme_{secrets.token_urlsafe(24)}",
        issued_at=issued_at,
        deadline=issued_at + MANAGEMENT_RPC_DEADLINE_SECONDS,
        operation=operation,
        channel_ref=EntityRef(f"{channel.id}@{channel.origin_domain}"),
        application_ref=EntityRef(f"{application_id}@{application_domain}"),
        actor=RemoteUserProfile.model_validate(profile_from_user(actor)),
        user_installation=user_installation,
        device_snapshot=device_snapshot,
    )
    result = await request_management_rpc(
        session,
        settings,
        authority_domain=conversation.authority_domain,
        path="/_kaede/v1/e2ee/dm-bots/manage",
        payload=request.model_dump(mode="json"),
        response_model=BotDME2EEAuthorityResult,
        response_matches=lambda response: (
            response.request_id == request.request_id
            and response.operation == request.operation
            and response.channel_ref == request.channel_ref
            and response.application_ref == request.application_ref
        ),
        label="dm-bot-e2ee",
        errors=_DM_BOT_E2EE_RPC_ERRORS,
        send=signed_request,
    )
    return {str(key): value for key, value in result.body.items()}


async def _sync_bot_participation_policy(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    channel: Channel,
    actor: User,
) -> None:
    if channel.encryption_mode == "e2ee" and channel.encryption_state == "active":
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


def _render_participations(
    rows: Sequence[tuple[BotE2EEParticipation, BotE2EEDevice]],
    installation: BotInstallation | BotDMCapability,
    channel: Channel,
) -> dict[str, object]:
    return {
        "application_ref": f"{installation.application_id}@{installation.application_domain}",
        "channel_ref": f"{channel.id}@{channel.origin_domain}",
        "e2ee_mode": installation.e2ee_mode,
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
    }


async def _list_bot_e2ee_participation(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    channel_ref: EntityRef,
    application_ref: EntityRef,
    actor: User,
) -> dict[str, object]:
    channel = await _authorize_bot_e2ee_management(
        session,
        redis,
        settings,
        guild,
        actor,
        channel_ref,
    )
    _, _, installation = await _bot_installation_for_e2ee(
        session,
        guild,
        application_ref,
        settings,
    )
    rows = list(
        (
            await session.execute(
                select(BotE2EEParticipation, BotE2EEDevice)
                .join(BotE2EEDevice, BotE2EEDevice.id == BotE2EEParticipation.device_id)
                .where(
                    BotE2EEParticipation.installation_id == installation.id,
                    BotE2EEParticipation.channel_id == channel.id,
                    BotE2EEParticipation.channel_domain == channel.origin_domain,
                )
                .order_by(BotE2EEDevice.protocol_id)
            )
        ).tuples()
    )
    return _render_participations(rows, installation, channel)


async def _grant_bot_e2ee_participation(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    channel_ref: EntityRef,
    application_ref: EntityRef,
    actor: User,
    reason: str | None,
) -> dict[str, object]:
    channel = await _authorize_bot_e2ee_management(
        session,
        redis,
        settings,
        guild,
        actor,
        channel_ref,
    )
    application, bot, installation = await _bot_installation_for_e2ee(
        session,
        guild,
        application_ref,
        settings,
    )
    _, devices = await _device_snapshot_for_installation(
        session,
        snowflake,
        settings,
        guild,
        channel,
        application,
        bot,
    )
    existing = list(
        await session.scalars(
            select(BotE2EEParticipation)
            .where(
                BotE2EEParticipation.installation_id == installation.id,
                BotE2EEParticipation.channel_id == channel.id,
                BotE2EEParticipation.channel_domain == channel.origin_domain,
            )
            .with_for_update()
        )
    )
    await _reconcile_bot_e2ee_participations(
        session,
        snowflake,
        existing=existing,
        devices=devices,
        channel=channel,
        actor=actor,
        owner_field="installation_id",
        owner_id=installation.id,
    )
    await _sync_bot_participation_policy(
        session,
        redis,
        settings,
        guild,
        channel,
        actor,
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        BOT_E2EE_AUDIT_ACTION_UPDATE_INTEGRATION,
        target_type="application",
        target_ref={"id": str(application.id), "domain": application.origin_domain},
        reason=normalize_audit_reason(reason),
        changes=[
            {
                "key": "e2ee_participation",
                "old_value": "revoked",
                "new_value": "pending",
            }
        ],
    )
    await materialize_updated_at(session, channel)
    rendered_channel = channel_payload(channel)
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "CHANNEL_UPDATE",
        rendered_channel,
    )
    return await _list_bot_e2ee_participation(
        session,
        redis,
        settings,
        guild,
        channel_ref,
        application_ref,
        actor,
    )


async def _revoke_bot_e2ee_participation(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    channel_ref: EntityRef,
    application_ref: EntityRef,
    actor: User,
    reason: str | None,
) -> dict[str, object]:
    channel = await _authorize_bot_e2ee_management(
        session,
        redis,
        settings,
        guild,
        actor,
        channel_ref,
    )
    application, _, installation = await _bot_installation_for_e2ee(
        session,
        guild,
        application_ref,
        settings,
    )
    rows = list(
        await session.scalars(
            select(BotE2EEParticipation)
            .where(
                BotE2EEParticipation.installation_id == installation.id,
                BotE2EEParticipation.channel_id == channel.id,
                BotE2EEParticipation.channel_domain == channel.origin_domain,
                BotE2EEParticipation.status.in_(("pending", "active")),
            )
            .with_for_update()
        )
    )
    if not rows:
        raise HTTPException(status_code=404, detail={"code": "BOT_E2EE_PARTICIPATION_NOT_FOUND"})
    now = datetime.now(UTC)
    for participation in rows:
        participation.status = "revoked"
        participation.revoked_at = now
        participation.consent_generation += 1
    await _sync_bot_participation_policy(
        session,
        redis,
        settings,
        guild,
        channel,
        actor,
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        BOT_E2EE_AUDIT_ACTION_UPDATE_INTEGRATION,
        target_type="application",
        target_ref={"id": str(application.id), "domain": application.origin_domain},
        reason=normalize_audit_reason(reason),
        changes=[
            {
                "key": "e2ee_participation",
                "old_value": "active",
                "new_value": "revoked",
            }
        ],
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    return _render_participations([], installation, channel)


async def _proxy_bot_e2ee_management(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
    actor: User,
    operation: Literal["bot_e2ee.list", "bot_e2ee.grant", "bot_e2ee.revoke"],
    channel_ref: EntityRef,
    application_ref: EntityRef,
    reason: str | None = None,
) -> dict[str, object] | None:
    result = await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        actor,
        operation,
        {
            "channel_ref": str(channel_ref),
            "application_ref": str(application_ref),
            "reason": normalize_audit_reason(reason),
        },
    )
    return cast(dict[str, object], result.body) if result is not None else None


async def _local_management_guild(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
) -> Guild:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    if guild is None or guild.unavailable or guild.origin_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return guild


@router.get("/api/v1/guilds/{guild_ref}/channels/{channel_ref}/e2ee/bots/{application_ref}")
async def get_bot_e2ee_participation(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    application_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    proxied = await _proxy_bot_e2ee_management(
        session,
        settings,
        guild_ref,
        auth.user,
        "bot_e2ee.list",
        channel_ref,
        application_ref,
    )
    if proxied is not None:
        return proxied
    guild = await _local_management_guild(session, settings, guild_ref)
    return await _list_bot_e2ee_participation(
        session,
        redis,
        settings,
        guild,
        channel_ref,
        application_ref,
        auth.user,
    )


@router.put("/api/v1/guilds/{guild_ref}/channels/{channel_ref}/e2ee/bots/{application_ref}")
async def grant_bot_e2ee_participation(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    application_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> dict[str, object]:
    proxied = await _proxy_bot_e2ee_management(
        session,
        settings,
        guild_ref,
        auth.user,
        "bot_e2ee.grant",
        channel_ref,
        application_ref,
        reason,
    )
    if proxied is not None:
        return proxied
    guild = await _local_management_guild(session, settings, guild_ref)
    return await _grant_bot_e2ee_participation(
        session,
        redis,
        snowflake,
        settings,
        guild,
        channel_ref,
        application_ref,
        auth.user,
        reason,
    )


@router.delete("/api/v1/guilds/{guild_ref}/channels/{channel_ref}/e2ee/bots/{application_ref}")
async def delete_bot_e2ee_participation(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    application_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> dict[str, object]:
    proxied = await _proxy_bot_e2ee_management(
        session,
        settings,
        guild_ref,
        auth.user,
        "bot_e2ee.revoke",
        channel_ref,
        application_ref,
        reason,
    )
    if proxied is not None:
        return proxied
    guild = await _local_management_guild(session, settings, guild_ref)
    return await _revoke_bot_e2ee_participation(
        session,
        redis,
        snowflake,
        settings,
        guild,
        channel_ref,
        application_ref,
        auth.user,
        reason,
    )


@router.post("/_kaede/v1/e2ee/dm-bots/manage")
async def federation_dm_bot_e2ee_management(
    payload: BotDME2EEAuthorityRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "dm-bot-e2ee-management",
        capacity=120,
        refill_per_minute=120,
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "FEDERATION_FORBIDDEN"})
    await consume_management_request_once(
        redis,
        settings,
        origin=principal.origin,
        namespace="dm-bot-e2ee",
        request_id=payload.request_id,
        issued_at=payload.issued_at,
        deadline=payload.deadline,
        now=int(time.time()),
        expired_code="KAED_FED_DM_BOT_E2EE_REQUEST_EXPIRED",
        replayed_code="KAED_FED_DM_BOT_E2EE_REQUEST_REPLAYED",
    )
    channel_id, channel_domain = payload.channel_ref.resolve(settings.domain)
    if channel_domain != settings.domain:
        raise HTTPException(status_code=403, detail={"code": "FEDERATION_FORBIDDEN"})
    actor = await upsert_remote_user(session, settings, payload.actor)
    if actor.account_type != "human" or actor.disabled_at is not None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if payload.operation == "grant":
        await require_remote_user_creation_allowed(session, actor)
    prepared = None
    target_destinations: set[str] = set()
    if payload.operation == "grant" and payload.user_installation is not None:
        assertion = payload.user_installation
        application_ref = assertion.application_ref.resolve(settings.domain)
        if application_ref != payload.application_ref.resolve(settings.domain):
            raise HTTPException(status_code=422, detail={"code": "USER_INSTALLATION_MISMATCH"})
        await require_federated_user_application(
            session,
            settings,
            snowflake,
            application_ref,
            assertion,
            refresh_remote_application=refresh_user_bot_application,
        )
        source_ref = int(assertion.id), principal.origin
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    federated_user_installation_lock(
                        source_ref[0],
                        source_ref[1],
                        application_ref,
                        actor,
                    )
                )
            )
        )
        installation = await locked_federated_user_installation(
            session,
            actor,
            application_ref,
            source_ref,
        )
        installation = await reconcile_federated_user_installation(
            session,
            snowflake,
            actor,
            application_ref,
            source_ref,
            assertion,
            installation,
            minimum_expires_at=datetime.fromtimestamp(payload.deadline, UTC),
            maximum_expires_at=(
                datetime.fromtimestamp(payload.issued_at, UTC) + USER_INSTALLATION_AUTHORITY_LEASE
            ),
            clock_skew=timedelta(seconds=settings.federation_clock_skew_seconds),
        )
        await session.flush()
        application, bot, installation = await _dm_user_installation_for_e2ee(
            session,
            settings,
            actor,
            payload.application_ref,
        )
        try:
            snapshot = await validated_bot_e2ee_snapshot(
                session,
                settings,
                application.origin_domain,
                cast(dict[str, object], payload.device_snapshot),
                application_id=application.id,
                bot_user_ref=(bot.id, bot.origin_domain),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "BOT_E2EE_DEVICE_SNAPSHOT_INVALID"},
            ) from exc
        current_generation = max(0, int(bot.e2ee_device_generation or 0))
        if int(snapshot.generation) < current_generation:
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_DEVICE_GENERATION_ROLLBACK"},
            )
        devices = await materialize_bot_e2ee_snapshot(
            session,
            snowflake,
            application,
            snapshot,
            known_generation=current_generation,
        )
        bot.e2ee_device_generation = int(snapshot.generation)
        if not devices:
            raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_DEVICE_REQUIRED"})
        prepared = application, bot, installation, devices
        target_destinations = await queue_application_target_snapshots_for_refs(
            session,
            settings,
            {application_ref},
        )
    if payload.operation == "get":
        body = await _get_dm_bot_e2ee(
            session,
            settings,
            actor,
            payload.channel_ref,
            payload.application_ref,
        )
    elif payload.operation == "grant":
        body = await _grant_dm_bot_e2ee(
            session,
            redis,
            snowflake,
            settings,
            actor,
            payload.channel_ref,
            payload.application_ref,
            prepared,
        )
    else:
        body = await _revoke_dm_bot_e2ee(
            session,
            redis,
            settings,
            actor,
            payload.channel_ref,
            payload.application_ref,
        )
    await wake_application_target_deliveries(target_destinations)
    return BotDME2EEAuthorityResult(
        request_id=payload.request_id,
        operation=payload.operation,
        channel_ref=payload.channel_ref,
        application_ref=payload.application_ref,
        body=body,
    ).model_dump(mode="json")


@router.get("/api/v1/channels/{channel_ref}/e2ee/bots/{application_ref}")
async def get_dm_bot_e2ee_participation(
    channel_ref: EntityRef,
    application_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    proxied = await _proxy_dm_bot_e2ee(
        session,
        settings,
        auth.user,
        channel_ref,
        application_ref,
        "get",
    )
    if proxied is not None:
        return proxied
    return await _get_dm_bot_e2ee(
        session,
        settings,
        auth.user,
        channel_ref,
        application_ref,
    )


@router.put("/api/v1/channels/{channel_ref}/e2ee/bots/{application_ref}")
async def grant_dm_bot_e2ee_participation(
    channel_ref: EntityRef,
    application_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    proxied = await _proxy_dm_bot_e2ee(
        session,
        settings,
        auth.user,
        channel_ref,
        application_ref,
        "grant",
    )
    if proxied is not None:
        return proxied
    return await _grant_dm_bot_e2ee(
        session,
        redis,
        snowflake,
        settings,
        auth.user,
        channel_ref,
        application_ref,
    )


@router.delete("/api/v1/channels/{channel_ref}/e2ee/bots/{application_ref}")
async def revoke_dm_bot_e2ee_participation(
    channel_ref: EntityRef,
    application_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    proxied = await _proxy_dm_bot_e2ee(
        session,
        settings,
        auth.user,
        channel_ref,
        application_ref,
        "revoke",
    )
    if proxied is not None:
        return proxied
    return await _revoke_dm_bot_e2ee(
        session,
        redis,
        settings,
        auth.user,
        channel_ref,
        application_ref,
    )


@router.get("/api/v1/bots/channels/{channel_ref}/e2ee/participation")
async def bot_e2ee_participation_status(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    # Import lazily because the public bot router imports this module's shared
    # participation verifier. Both surfaces still use one exact channel-grant
    # resolver and therefore cannot disagree on capability lineage.
    from app.api.bots import installation_for_channel

    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    resource = await session.get(Channel, (channel_id, channel_domain))
    if resource is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "dm.send" if resource.guild_id is None else "guilds.read",
    )
    statement = select(BotE2EEParticipation, BotE2EEDevice).join(
        BotE2EEDevice,
        BotE2EEDevice.id == BotE2EEParticipation.device_id,
    )
    if isinstance(installation, BotDMCapability):
        statement = statement.join(
            BotDMGrant,
            BotDMGrant.id == BotE2EEParticipation.dm_grant_id,
        ).where(BotDMGrant.dm_capability_id == installation.id)
    else:
        statement = statement.where(BotE2EEParticipation.installation_id == installation.id)
    rows = list(
        (
            await session.execute(
                statement.where(
                    BotE2EEParticipation.channel_id == channel.id,
                    BotE2EEParticipation.channel_domain == channel.origin_domain,
                ).order_by(BotE2EEDevice.protocol_id)
            )
        ).tuples()
    )
    return _render_participations(rows, installation, channel) | {
        "encryption_policy": channel_encryption_policy_payload(channel)
    }


@router.get("/api/v1/bots/channels/{channel_ref}/e2ee/control-log")
async def bot_e2ee_control_log(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    after: EntityRef | None = None,
    limit: int = Query(default=25, ge=1, le=25),
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    """Return durable, ordered MLS controls for one exact bot device.

    Gateway delivery is intentionally not the recovery authority for Welcome
    and Commit records.  A worker reconnecting after Redis retention has
    elapsed replays this sparse database log before acknowledging encrypted
    content, while the current installation/capability and participation
    checks prevent a revoked device from using it as historical access.
    """

    from app.api.bots import installation_for_channel

    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "messages.history",
        installation_id,
    )
    if channel.guild_id is not None:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is None:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        await require_permissions(
            session,
            redis,
            guild,
            principal.user,
            Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY,
            channel=channel,
        )
    participation, device = await require_bot_e2ee_participation(
        session,
        installation,
        channel,
        e2ee_device_id,
        worker_id=principal.worker.id,
    )
    conditions = [
        E2EEControlRecord.channel_id == channel.id,
        E2EEControlRecord.channel_domain == channel.origin_domain,
        E2EEControlRecord.id >= channel.created_floor_id,
        E2EEControlRecord.origin_domain == settings.domain,
        E2EEControlRecord.room_operation_id.is_not(None),
        E2EEControlRecord.room_operation_domain == settings.domain,
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
            raise HTTPException(status_code=409, detail={"code": "E2EE_HISTORY_FLOOR_INVALID"})
        conditions.append(
            tuple_(E2EEControlRecord.id, E2EEControlRecord.origin_domain)
            > (floor.id, floor.origin_domain)
        )
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
        if after_domain != settings.domain:
            raise HTTPException(status_code=422, detail={"code": "E2EE_CONTROL_CURSOR_INVALID"})
        conditions.append(
            tuple_(E2EEControlRecord.id, E2EEControlRecord.origin_domain) > (after_id, after_domain)
        )
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
        "application_ref": f"{principal.application.id}@{principal.application.origin_domain}",
        "channel_ref": f"{channel.id}@{channel.origin_domain}",
        "device_id": device.protocol_id,
        "controls": controls,
        "next_after": next_after,
    }


async def dispatch_bot_e2ee_management(
    request: GuildManagementRequest,
    actor: User,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> dict[str, object]:
    payload = BotE2EEManagementPayload.model_validate(request.payload)
    if payload.application_ref is None:
        raise HTTPException(status_code=422, detail={"code": "APPLICATION_REF_REQUIRED"})
    guild = await _local_management_guild(
        session,
        settings,
        EntityRef(f"{request.guild.id}@{request.guild.domain}"),
    )
    if request.operation == "bot_e2ee.list":
        body = await _list_bot_e2ee_participation(
            session,
            redis,
            settings,
            guild,
            payload.channel_ref,
            payload.application_ref,
            actor,
        )
    elif request.operation == "bot_e2ee.grant":
        body = await _grant_bot_e2ee_participation(
            session,
            redis,
            snowflake,
            settings,
            guild,
            payload.channel_ref,
            payload.application_ref,
            actor,
            payload.reason,
        )
    elif request.operation == "bot_e2ee.revoke":
        body = await _revoke_bot_e2ee_participation(
            session,
            redis,
            snowflake,
            settings,
            guild,
            payload.channel_ref,
            payload.application_ref,
            actor,
            payload.reason,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail={"code": "KAED_FED_GUILD_MANAGEMENT_OPERATION_UNSUPPORTED"},
        )
    return body
