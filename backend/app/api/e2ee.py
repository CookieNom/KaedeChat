from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, tuple_
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
    ACCOUNT_VAULT_LEASE_TTL_SECONDS,
    E2EE_PROTOCOL_MLS_10,
    E2EE_SUITE_MLS_128,
    RELEASE_ACCOUNT_VAULT_LEASE,
    account_vault_lease_key,
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
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.models import (
    Channel,
    DMConversation,
    E2EEAccountVault,
    E2EEAccountVaultDigest,
    E2EEControlRecord,
    E2EEDevice,
    E2EEKeyPackage,
    E2EEPackageClaimBatch,
    E2EERoomOperation,
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
RECOVERY_AUTHORIZATION_TTL_SECONDS = 300
RECOVERY_AUTHORIZATION_PREFIX = "ker_"
KEY_PACKAGE_MAX_LIFETIME = timedelta(days=30)
# All signed-in clients synchronize one portable account MLS identity through
# the opaque account vault. A second active identity would fork the account.
MAX_ACTIVE_DEVICES = 1
MAX_AVAILABLE_KEY_PACKAGES_PER_DEVICE = 100
MAX_ROOM_E2EE_MEMBERS = 500
MAX_ROOM_E2EE_DEVICES = 48
DEVICE_CAPABILITIES = frozenset({"e2ee-mls/1", "e2ee-media/1"})
ROOM_OPERATION_TTL = timedelta(minutes=30)
MAX_ACCOUNT_VAULT_CIPHERTEXT_BYTES = 32 * 1024 * 1024 + 16


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
    recovery_authorization: str | None = Field(
        default=None,
        min_length=47,
        max_length=47,
    )

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

    @field_validator("recovery_authorization")
    @classmethod
    def canonical_recovery_authorization(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(RECOVERY_AUTHORIZATION_PREFIX):
            raise ValueError("recovery authorization is invalid")
        decode_base64url(value[len(RECOVERY_AUTHORIZATION_PREFIX) :], size=32)
        return value


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
    operation_id: str = Field(pattern=r"^keo_[A-Za-z0-9_-]{43}$")
    sender_device_id: str = Field(pattern=r"^ked_[A-Za-z0-9_-]{43}$")


class RoomActivationRequest(RoomProposalRequest):
    policy_generation: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    epoch: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    group_id: str = Field(min_length=43, max_length=43)
    commit: str = Field(min_length=2, max_length=87_384)
    welcome: str = Field(min_length=2, max_length=87_384)
    prepared_vault_revision: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    prepared_vault_digest: str = Field(min_length=43, max_length=43)
    vault_lease_token: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )

    @field_validator("commit", "welcome")
    @classmethod
    def bounded_mls_message(cls, value: str) -> str:
        decode_base64url(value, maximum=64 * 1024)
        return value

    @field_validator("group_id", "prepared_vault_digest")
    @classmethod
    def canonical_32_byte_value(cls, value: str) -> str:
        decode_base64url(value, size=32)
        return value


class RoomRekeyActivationRequest(RoomActivationRequest):
    pass


class AccountVaultEnvelope(RequestModel):
    version: int
    cipher: str
    sequence: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    nonce: str = Field(min_length=16, max_length=16)
    ciphertext: str = Field(min_length=23, max_length=44_739_264)

    @model_validator(mode="after")
    def valid_envelope(self) -> AccountVaultEnvelope:
        if (
            self.version != 2
            or self.cipher != "AES-256-GCM"
            or int(self.sequence) > 9_223_372_036_854_775_807
        ):
            raise ValueError("unsupported account vault envelope")
        decode_base64url(self.nonce, size=12)
        decode_base64url(
            self.ciphertext,
            maximum=MAX_ACCOUNT_VAULT_CIPHERTEXT_BYTES,
        )
        return self


class AccountVaultWrite(RequestModel):
    lease_token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    expected_revision: str = Field(pattern=r"^(0|[1-9][0-9]{0,18})$")
    envelope: AccountVaultEnvelope

    @model_validator(mode="after")
    def sequence_follows_cas_revision(self) -> AccountVaultWrite:
        expected = int(self.expected_revision)
        if expected >= 9_223_372_036_854_775_807 or int(self.envelope.sequence) != expected + 1:
            raise ValueError("account vault sequence does not follow expected revision")
        return self


class AccountVaultLeaseRelease(RequestModel):
    lease_token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class AccountEncryptionReset(RequestModel):
    confirmation: str

    @field_validator("confirmation")
    @classmethod
    def exact_confirmation(cls, value: str) -> str:
        if value != "RESET ENCRYPTED HISTORY":
            raise ValueError("encryption reset confirmation does not match")
        return value


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


def require_portable_identity_slot(active_count: int) -> None:
    if active_count >= MAX_ACTIVE_DEVICES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "E2EE_DEVICE_LIMIT_REACHED"},
        )


def recovery_authorization_digest(value: str) -> bytes:
    if not value.startswith(RECOVERY_AUTHORIZATION_PREFIX):
        raise ValueError("recovery authorization is invalid")
    encoded = value[len(RECOVERY_AUTHORIZATION_PREFIX) :]
    decode_base64url(encoded, size=32)
    return hashlib.sha256(
        b"kaede-e2ee-recovery-authorization-v1\0" + value.encode("ascii")
    ).digest()


def issue_recovery_authorization(user: User, session_id: str, now: datetime) -> str:
    authorization = RECOVERY_AUTHORIZATION_PREFIX + secrets.token_urlsafe(32)
    user.e2ee_recovery_token_hash = recovery_authorization_digest(authorization)
    user.e2ee_recovery_session_id = session_id
    user.e2ee_recovery_generation = user.e2ee_device_generation
    user.e2ee_recovery_expires_at = now + timedelta(seconds=RECOVERY_AUTHORIZATION_TTL_SECONDS)
    return authorization


def clear_recovery_authorization(user: User) -> None:
    user.e2ee_recovery_token_hash = None
    user.e2ee_recovery_session_id = None
    user.e2ee_recovery_generation = None
    user.e2ee_recovery_expires_at = None


def consume_recovery_authorization(user: User) -> None:
    """Retain the session fence while making its recovery bearer one-time.

    Device registration and the first replacement-vault write are separate
    transactions and either may happen first. Once the device half commits,
    replace the bearer digest with an unknowable tombstone while retaining the
    session/generation/expiry fence until the vault half also commits.
    """

    user.e2ee_recovery_token_hash = secrets.token_bytes(32)
    user.e2ee_recovery_generation = user.e2ee_device_generation


def recovery_authorization_state(
    user: User,
) -> tuple[bytes, str, int, datetime] | None:
    """Return the complete durable recovery fence, failing closed on corruption."""

    token_hash = user.e2ee_recovery_token_hash
    recovery_session_id = user.e2ee_recovery_session_id
    recovery_generation = user.e2ee_recovery_generation
    expires_at = user.e2ee_recovery_expires_at
    if (
        token_hash is None
        and recovery_session_id is None
        and recovery_generation is None
        and expires_at is None
    ):
        return None
    if (
        token_hash is None
        or recovery_session_id is None
        or recovery_generation is None
        or expires_at is None
    ):
        raise RuntimeError("E2EE recovery authorization state is incomplete")
    return token_hash, recovery_session_id, recovery_generation, expires_at


def require_recovery_enrollment_session(
    user: User,
    session_id: str,
    now: datetime,
) -> bytes | None:
    """Fence the first post-reset identity enrollment to its initiating session."""

    recovery_state = recovery_authorization_state(user)
    if recovery_state is None:
        return None
    token_hash, recovery_session_id, recovery_generation, expires_at = recovery_state
    if (
        expires_at <= now
        or recovery_generation != user.e2ee_device_generation
        or not secrets.compare_digest(recovery_session_id, session_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"},
        )
    return token_hash


def require_recovery_reset_session(user: User, session_id: str, now: datetime) -> None:
    """Prevent another still-signed-in client from stealing an active reset."""

    recovery_state = recovery_authorization_state(user)
    if recovery_state is None:
        return
    _, recovery_session_id, _, expires_at = recovery_state
    if expires_at > now and not secrets.compare_digest(recovery_session_id, session_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"},
        )


def require_recovery_authorization(
    user: User,
    session_id: str,
    authorization: str | None,
    now: datetime,
) -> None:
    pending_hash = require_recovery_enrollment_session(user, session_id, now)
    if pending_hash is None or authorization is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"},
        )
    candidate = recovery_authorization_digest(authorization)
    if not secrets.compare_digest(pending_hash, candidate):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"},
        )


def recovery_fence_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"},
    )


async def finish_recovery_device_artifact(
    session: AsyncSession,
    user: User,
    session_id: str,
) -> None:
    """Commit the device half and clear the fence only if vault revision 1 exists."""

    recovery_state = recovery_authorization_state(user)
    if recovery_state is None:
        return
    _, recovery_session_id, _, _ = recovery_state
    if not secrets.compare_digest(recovery_session_id, session_id):
        raise recovery_fence_conflict()
    vault_revision = await session.scalar(
        select(E2EEAccountVault.revision).where(
            E2EEAccountVault.user_id == user.id,
            E2EEAccountVault.user_domain == user.origin_domain,
        )
    )
    if vault_revision == 1:
        clear_recovery_authorization(user)
        return
    if vault_revision is not None:
        # A pending reset begins a new chain at revision one. Do not silently
        # bless an impossible partial transition; another explicit reset is
        # required to recover from corrupt or unsupported state.
        raise recovery_fence_conflict()
    consume_recovery_authorization(user)


async def finish_recovery_vault_artifact(
    session: AsyncSession,
    user: User,
    session_id: str,
    vault_revision: int,
) -> None:
    """Commit the vault half and clear the fence only for its exact device half."""

    recovery_state = recovery_authorization_state(user)
    if recovery_state is None:
        return
    _, recovery_session_id, recovery_generation, _ = recovery_state
    if (
        vault_revision != 1
        or recovery_generation != user.e2ee_device_generation
        or not secrets.compare_digest(recovery_session_id, session_id)
    ):
        raise recovery_fence_conflict()
    active_devices = list(
        await session.scalars(
            select(E2EEDevice)
            .where(
                E2EEDevice.user_id == user.id,
                E2EEDevice.user_domain == user.origin_domain,
                E2EEDevice.revoked_at.is_(None),
            )
            .order_by(E2EEDevice.id)
            .with_for_update()
        )
    )
    if not active_devices:
        # Vault-first recovery remains fenced until device publication.
        return
    if len(active_devices) != 1:
        raise recovery_fence_conflict()
    device = active_devices[0]
    if (
        device.device_generation != recovery_generation
        or not isinstance(device.registered_session_id, str)
        or not secrets.compare_digest(device.registered_session_id, recovery_session_id)
    ):
        raise recovery_fence_conflict()
    clear_recovery_authorization(user)


def account_vault_digest(vault: E2EEAccountVault) -> bytes:
    """Bind a room operation to the exact opaque vault bytes persisted first."""

    return hashlib.sha256(
        b"kaede-account-vault-envelope-v2\0"
        + vault.format_version.to_bytes(2, "big")
        + vault.revision.to_bytes(8, "big")
        + vault.nonce
        + vault.ciphertext
    ).digest()


def account_vault_chain_root(parent: bytes, revision: int, digest: bytes) -> bytes:
    """Compute the cross-client compact opaque-vault ancestry commitment."""

    if len(parent) != 32 or len(digest) != 32 or not 0 < revision < 2**63:
        raise ValueError("invalid account-vault chain input")
    return hashlib.sha256(
        b"kaede-account-vault-chain-v2\0" + parent + revision.to_bytes(8, "big") + digest
    ).digest()


def render_account_vault(vault: E2EEAccountVault | None) -> dict[str, object] | None:
    if vault is None:
        return None
    return {
        "revision": str(vault.revision),
        "envelope": {
            "version": vault.format_version,
            "cipher": "AES-256-GCM",
            "sequence": str(vault.revision),
            "nonce": encode_base64url(vault.nonce),
            "ciphertext": encode_base64url(vault.ciphertext),
        },
        "digest": encode_base64url(account_vault_digest(vault)),
        "updated_at": vault.updated_at.isoformat(),
    }


def redis_text(value: object) -> str | None:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return value if isinstance(value, str) else None


def protocol_request_digest(label: str, value: dict[str, object]) -> bytes:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(label.encode("ascii") + b"\0" + encoded).digest()


async def require_prepared_account_vault(
    session: AsyncSession,
    redis: Redis,
    actor: User,
    *,
    lease_token: str | None,
    revision: str,
    digest: str,
) -> None:
    """Fence and verify the lease plus the exact saved vault context.

    The SQL user lock is the durable fencing point. Redis is deliberately
    re-read only after that lock is acquired, so a stale holder that outlived
    its TTL cannot resume after a reset or a newer lease holder commits.
    """

    if not actor.is_local or lease_token is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_ACCOUNT_VAULT_ATTESTATION_REQUIRED"},
        )
    locked_actor = await session.scalar(
        select(User)
        .where(User.id == actor.id, User.origin_domain == actor.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_actor is None or not locked_actor.is_local:
        raise RuntimeError("local E2EE vault owner disappeared")
    current_token = redis_text(
        await redis.get(account_vault_lease_key(locked_actor.id, locked_actor.origin_domain))
    )
    if current_token is None or not secrets.compare_digest(current_token, lease_token):
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_ACCOUNT_VAULT_LEASE_EXPIRED"},
        )
    vault = await session.scalar(
        select(E2EEAccountVault)
        .where(
            E2EEAccountVault.user_id == locked_actor.id,
            E2EEAccountVault.user_domain == locked_actor.origin_domain,
        )
        .with_for_update()
    )
    expected_digest = decode_base64url(digest, size=32)
    if (
        vault is None
        or vault.revision != int(revision)
        or not secrets.compare_digest(account_vault_digest(vault), expected_digest)
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_ACCOUNT_VAULT_CONTEXT_MISMATCH"},
        )


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


@router.get("/vault")
async def get_account_vault(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    vault = await session.get(
        E2EEAccountVault,
        (auth.user.id, auth.user.origin_domain),
    )
    return {"vault": render_account_vault(vault)}


@router.get("/vault/digests")
async def get_account_vault_digests(
    after: int = Query(default=0, ge=0, le=9_223_372_036_854_775_807),
    limit: int = Query(default=256, ge=1, le=256),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Return a strict, ascending page of compact vault commitments."""

    rows = list(
        await session.scalars(
            select(E2EEAccountVaultDigest)
            .where(
                E2EEAccountVaultDigest.user_id == auth.user.id,
                E2EEAccountVaultDigest.user_domain == auth.user.origin_domain,
                E2EEAccountVaultDigest.revision > after,
            )
            .order_by(E2EEAccountVaultDigest.revision)
            .limit(limit + 1)
        )
    )
    page = rows[:limit]
    expected_revision = after + 1
    for row in page:
        if row.revision != expected_revision:
            # A gap means the append-only rollback ledger is corrupt. Never
            # offer a client a partial ancestry proof it could misinterpret.
            raise RuntimeError("account-vault digest ledger is not consecutive")
        expected_revision += 1
    return {
        "digests": [
            {
                "revision": str(row.revision),
                "digest": encode_base64url(row.digest),
            }
            for row in page
        ],
        "next_after": str(page[-1].revision) if page and len(rows) > limit else None,
    }


@router.post("/vault/lease")
async def acquire_account_vault_lease(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> dict[str, object]:
    locked_user = await session.scalar(
        select(User)
        .where(User.id == auth.user.id, User.origin_domain == auth.user.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_user is None or not locked_user.is_local:
        raise RuntimeError("local E2EE vault owner disappeared")
    # Reset deletes the old vault before returning its one-time authorization.
    # Until replacement enrollment commits, only that initiating auth session
    # may obtain a lease capable of repopulating the account vault.
    require_recovery_enrollment_session(
        locked_user,
        auth.grant.session_id,
        datetime.now(UTC),
    )
    token = secrets.token_urlsafe(32)
    acquired = await redis.set(
        account_vault_lease_key(locked_user.id, locked_user.origin_domain),
        token,
        ex=ACCOUNT_VAULT_LEASE_TTL_SECONDS,
        nx=True,
    )
    if not acquired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "E2EE_ACCOUNT_VAULT_BUSY", "retry_after": 1},
            headers={"Retry-After": "1"},
        )
    vault = await session.get(
        E2EEAccountVault,
        (locked_user.id, locked_user.origin_domain),
    )
    return {
        "lease_token": token,
        "expires_in": ACCOUNT_VAULT_LEASE_TTL_SECONDS,
        "vault": render_account_vault(vault),
    }


@router.put("/vault")
async def update_account_vault(
    payload: AccountVaultWrite,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> dict[str, object]:
    locked_user = await session.scalar(
        select(User)
        .where(User.id == auth.user.id, User.origin_domain == auth.user.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_user is None or not locked_user.is_local:
        raise RuntimeError("local E2EE vault owner disappeared")
    require_recovery_enrollment_session(
        locked_user,
        auth.grant.session_id,
        datetime.now(UTC),
    )
    current_token = redis_text(
        await redis.get(account_vault_lease_key(locked_user.id, locked_user.origin_domain))
    )
    if current_token is None or not secrets.compare_digest(current_token, payload.lease_token):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "E2EE_ACCOUNT_VAULT_LEASE_EXPIRED"},
        )
    expected_revision = int(payload.expected_revision)
    vault = await session.scalar(
        select(E2EEAccountVault)
        .where(
            E2EEAccountVault.user_id == auth.user.id,
            E2EEAccountVault.user_domain == auth.user.origin_domain,
        )
        .with_for_update()
    )
    if vault is None:
        if expected_revision != 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "E2EE_ACCOUNT_VAULT_REVISION_CONFLICT"},
            )
        vault = E2EEAccountVault(
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
            user_is_local=True,
            revision=1,
            format_version=2,
            nonce=decode_base64url(payload.envelope.nonce, size=12),
            ciphertext=decode_base64url(
                payload.envelope.ciphertext,
                maximum=MAX_ACCOUNT_VAULT_CIPHERTEXT_BYTES,
            ),
        )
        session.add(vault)
    else:
        if vault.revision != expected_revision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "E2EE_ACCOUNT_VAULT_REVISION_CONFLICT"},
            )
        vault.revision += 1
        vault.format_version = 2
        vault.nonce = decode_base64url(payload.envelope.nonce, size=12)
        vault.ciphertext = decode_base64url(
            payload.envelope.ciphertext,
            maximum=MAX_ACCOUNT_VAULT_CIPHERTEXT_BYTES,
        )
        vault.updated_at = datetime.now(UTC)
    await session.flush()
    session.add(
        E2EEAccountVaultDigest(
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
            user_is_local=True,
            revision=vault.revision,
            digest=account_vault_digest(vault),
        )
    )
    await finish_recovery_vault_artifact(
        session,
        locked_user,
        auth.grant.session_id,
        vault.revision,
    )
    await session.commit()
    return {"vault": render_account_vault(vault)}


@router.post("/vault/lease/release", status_code=status.HTTP_204_NO_CONTENT)
async def release_account_vault_lease(
    payload: AccountVaultLeaseRelease,
    auth: AuthenticatedUser = Depends(require_user),
    redis: Redis = Depends(get_redis),
) -> Response:
    await cast(
        Awaitable[object],
        redis.eval(
            RELEASE_ACCOUNT_VAULT_LEASE,
            1,
            account_vault_lease_key(auth.user.id, auth.user.origin_domain),
            payload.lease_token,
        ),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reset")
async def reset_account_encryption(
    payload: AccountEncryptionReset,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str | int]:
    del payload
    lease_key = account_vault_lease_key(auth.user.id, auth.user.origin_domain)
    lease_token = secrets.token_urlsafe(32)
    if not await redis.set(
        lease_key,
        lease_token,
        ex=ACCOUNT_VAULT_LEASE_TTL_SECONDS,
        nx=True,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "E2EE_ACCOUNT_VAULT_BUSY", "retry_after": 1},
            headers={"Retry-After": "1"},
        )
    try:
        locked_user = await session.scalar(
            select(User)
            .where(
                User.id == auth.user.id,
                User.origin_domain == auth.user.origin_domain,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_user is None:
            raise RuntimeError("authenticated user disappeared")
        current_token = redis_text(await redis.get(lease_key))
        if current_token is None or not secrets.compare_digest(current_token, lease_token):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "E2EE_ACCOUNT_VAULT_LEASE_EXPIRED"},
            )
        now = datetime.now(UTC)
        require_recovery_reset_session(
            locked_user,
            auth.grant.session_id,
            now,
        )
        devices = list(
            await session.scalars(
                select(E2EEDevice)
                .where(
                    E2EEDevice.user_id == locked_user.id,
                    E2EEDevice.user_domain == locked_user.origin_domain,
                    E2EEDevice.revoked_at.is_(None),
                )
                .with_for_update()
            )
        )
        for device in devices:
            device.revoked_at = now
        await session.execute(
            delete(E2EEKeyPackage).where(
                E2EEKeyPackage.device_id.in_([device.id for device in devices])
            )
        )
        await session.execute(
            delete(E2EEAccountVaultDigest).where(
                E2EEAccountVaultDigest.user_id == locked_user.id,
                E2EEAccountVaultDigest.user_domain == locked_user.origin_domain,
            )
        )
        await session.execute(
            delete(E2EEAccountVault).where(
                E2EEAccountVault.user_id == locked_user.id,
                E2EEAccountVault.user_domain == locked_user.origin_domain,
            )
        )
        locked_user.e2ee_device_generation += 1
        recovery_authorization = issue_recovery_authorization(
            locked_user,
            auth.grant.session_id,
            now,
        )
        paused_channels = await pause_local_e2ee_for_device_change(session, settings, locked_user)
        delivery_wakes = await queue_device_change_updates(
            session,
            settings,
            locked_user,
            paused_channels,
        )
        await session.commit()
    finally:
        await cast(
            Awaitable[object],
            redis.eval(RELEASE_ACCOUNT_VAULT_LEASE, 1, lease_key, lease_token),
        )
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
            "reset": True,
        },
    )
    return {
        "status": "encryption_reset",
        "account_ref": f"{locked_user.id}@{locked_user.origin_domain}",
        "recovery_authorization": recovery_authorization,
        "recovery_authorization_expires_in": RECOVERY_AUTHORIZATION_TTL_SECONDS,
    }


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
        .execution_options(populate_existing=True)
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
        if existing.credential != credential:
            raise HTTPException(status_code=409, detail={"code": "E2EE_DEVICE_IDENTITY_CONFLICT"})
        if existing.revoked_at is not None:
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
            # A recovery bundle contains the private key needed to answer the
            # fresh session-bound challenge above. The separate one-time
            # reset authorization prevents an older signed-in client holding
            # the same portable key from winning the reset/recovery race.
            require_portable_identity_slot(active_count)
            now = datetime.now(UTC)
            require_recovery_authorization(
                locked_user,
                auth.grant.session_id,
                payload.recovery_authorization,
                now,
            )
            locked_user.e2ee_device_generation += 1
            existing.device_generation = locked_user.e2ee_device_generation
            existing.revoked_at = None
            existing.registered_session_id = auth.grant.session_id
            existing.trust_state = "unverified"
            existing.last_seen_at = now
            existing.device_name = payload.device_name
            existing.platform = payload.platform
            existing.capabilities = payload.capabilities
            await finish_recovery_device_artifact(
                session,
                locked_user,
                auth.grant.session_id,
            )
            paused_channels = await pause_local_e2ee_for_device_change(
                session, settings, locked_user
            )
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
                    "device_id": existing.id,
                    "recovered": True,
                },
            )
            return render_device(existing, own=True)
        existing.last_seen_at = datetime.now(UTC)
        existing.device_name = payload.device_name
        existing.platform = payload.platform
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
    # Identical-identity registration retries returned above. Only a distinct
    # identity reaches this slot check.
    require_portable_identity_slot(active_count)
    require_recovery_enrollment_session(
        locked_user,
        auth.grant.session_id,
        datetime.now(UTC),
    )
    # A fresh replacement identity does not need the bearer, but it is still
    # fenced to the reset-initiating session. The fence is finalized only
    # after both the replacement device and the revision-one vault exist.
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
    await finish_recovery_device_artifact(
        session,
        locked_user,
        auth.grant.session_id,
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
        .execution_options(populate_existing=True)
    )
    if locked_user is not None:
        # Revocation changes the generation authenticated by a pending reset.
        # A stale signed-in session must not invalidate that fence and then
        # supersede the in-progress recovery with its older portable state.
        require_recovery_enrollment_session(
            locked_user,
            auth.grant.session_id,
            datetime.now(UTC),
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


@router.get("/channels/{channel_id}/control-log")
async def room_encryption_control_log(
    channel_id: EntityRef,
    after: EntityRef | None = None,
    limit: int = Query(default=25, ge=1, le=25),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return every durable MLS control record in ascending order.

    A Welcome or commit must not disappear merely because more than one chat
    page was written before a new client first opens the room. This narrow log
    is authorized exactly like message history, but returns no plaintext,
    profiles, reactions, or attachments.
    """

    access = await load_channel_access(session, settings, auth.user, channel_id)
    if access.guild is not None:
        await require_permissions(
            session,
            redis,
            access.guild,
            auth.user,
            required_permissions("message.list"),
            channel=access.channel,
        )
    channel = access.channel
    conditions = [
        E2EEControlRecord.channel_id == channel.id,
        E2EEControlRecord.channel_domain == channel.origin_domain,
        E2EEControlRecord.id >= channel.created_floor_id,
    ]
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
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
    controls: list[dict[str, object]] = []
    for message in candidates[:limit]:
        envelope = message.envelope if isinstance(message.envelope, dict) else None
        if envelope is None or envelope.get("operation") not in {"welcome", "commit"}:
            continue
        controls.append(
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(message.channel_id),
                "channel_domain": message.channel_domain,
                "author_id": str(message.author_id),
                "author_domain": message.author_domain,
                "e2ee": envelope,
                "encryption_policy_generation": str(message.policy_generation),
                "encryption_epoch": str(message.epoch),
                "apply": message.apply_mode != "audit",
                "room_operation_id": message.room_operation_id,
                "room_operation_domain": message.room_operation_domain,
            }
        )
    next_after = None
    if len(candidates) > limit:
        cursor = candidates[limit - 1]
        next_after = f"{cursor.id}@{cursor.origin_domain}"
    return {"controls": controls, "next_after": next_after}


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


def validate_remote_room_commit_response(
    rendered: dict[str, object],
    operation_status: dict[str, object],
    *,
    kind: str,
    operation_id: str,
    channel: Channel,
    policy_generation: str,
    group_id: str,
    authority: str,
) -> None:
    """Bind an authority commit response to the operation the actor home approved.

    A remote authority is authoritative for the room, but it must not be able
    to make the actor home project an unrelated operation or policy from a
    malformed/replayed response. The status record also binds the group ID to
    the authority's durable prepared proposal.
    """

    prepared = operation_status.get("prepared")
    prepared_policy = prepared.get("policy") if isinstance(prepared, dict) else None
    committed = operation_status.get("committed")
    controls = rendered.get("controls")
    valid_controls = (
        isinstance(controls, list)
        and len(controls) == 2
        and all(isinstance(item, dict) for item in controls)
    )
    if valid_controls:
        control_items = cast(list[dict[str, object]], controls)
        welcome = control_items[0]
        commit = control_items[1]
        valid_controls = (
            set(welcome) == {"id", "origin_domain", "operation", "apply"}
            and set(commit) == {"id", "origin_domain", "operation", "apply"}
            and welcome.get("operation") == "welcome"
            and welcome.get("apply") is True
            and commit.get("operation") == "commit"
            and commit.get("apply") is False
            and welcome.get("origin_domain") == authority
            and commit.get("origin_domain") == authority
            and welcome.get("id") != commit.get("id")
            and all(
                isinstance(item.get("id"), str)
                and bool(re.fullmatch(r"[1-9][0-9]{0,18}", cast(str, item["id"])))
                for item in (welcome, commit)
            )
        )
    rendered_group_id = rendered.get("encryption_group_id")
    valid_group = isinstance(rendered_group_id, str) and rendered_group_id == group_id
    if valid_group:
        try:
            decode_base64url(cast(str, rendered_group_id), size=32)
        except ValueError:
            valid_group = False
    if (
        kind not in {"activate", "rekey"}
        or operation_status.get("operation_id") != operation_id
        or operation_status.get("kind") != kind
        or operation_status.get("status") != "committed"
        or not isinstance(prepared, dict)
        or prepared.get("operation_id") != operation_id
        or prepared.get("status") != "prepared"
        or not isinstance(prepared_policy, dict)
        or prepared_policy.get("mode") != ("plaintext" if kind == "activate" else "e2ee")
        or prepared_policy.get("state") != ("proposed" if kind == "activate" else "rekeying")
        or prepared_policy.get("generation") != policy_generation
        or prepared_policy.get("group_id") != group_id
        or prepared_policy.get("protocol") != E2EE_PROTOCOL_MLS_10
        or prepared_policy.get("suite") != E2EE_SUITE_MLS_128
        or prepared_policy.get("epoch") is not None
        or committed != rendered
        or rendered.get("operation_id") != operation_id
        or rendered.get("operation_status") != "committed"
        or rendered.get("id") != str(channel.id)
        or rendered.get("origin_domain") != channel.origin_domain
        or rendered.get("encryption_mode") != "e2ee"
        or rendered.get("encryption_state") != "active"
        or rendered.get("encryption_policy_generation") != policy_generation
        or rendered.get("encryption_protocol") != E2EE_PROTOCOL_MLS_10
        or rendered.get("encryption_suite") != E2EE_SUITE_MLS_128
        or rendered.get("encryption_epoch") != "1"
        or not valid_group
        or not valid_controls
    ):
        raise HTTPException(
            status_code=502,
            detail={"code": "E2EE_ROOM_AUTHORITY_INVALID_RESPONSE"},
        )


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
    operation_id: str,
    operation_domain: str,
    channel_ref: tuple[int, str],
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
        locked_participant = await session.scalar(
            select(User)
            .where(
                User.id == participant.id,
                User.origin_domain == participant.origin_domain,
                User.is_local.is_(True),
            )
            .with_for_update()
        )
        if locked_participant is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "E2EE_PARTICIPANT_DEVICE_MISSING"},
            )
        remaining = max_devices - len(claimed)
        if remaining <= 0:
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_ROOM_DEVICE_LIMIT"},
            )
        request_digest = protocol_request_digest(
            "kaede-e2ee-package-claim-v2",
            {
                "operation_id": operation_id,
                "operation_domain": operation_domain,
                "channel_id": str(channel_ref[0]),
                "channel_domain": channel_ref[1],
                "claimant_id": str(claimant_ref[0]),
                "claimant_domain": claimant_ref[1],
                "target_id": str(participant.id),
                "target_domain": participant.origin_domain,
                "excluded_device_id": excluded_device_id or None,
                "max_devices": remaining,
            },
        )
        batch = await session.get(
            E2EEPackageClaimBatch,
            (
                operation_id,
                operation_domain,
                participant.id,
                participant.origin_domain,
            ),
        )
        if batch is not None:
            if not secrets.compare_digest(batch.request_digest, request_digest):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "E2EE_OPERATION_CONFLICT"},
                )
            stored = batch.response.get("key_packages")
            if not isinstance(stored, list):
                raise RuntimeError("persisted E2EE package claim is invalid")
            try:
                claimed.extend(
                    validate_claimed_package(
                        item,
                        expected_user=(participant.id, participant.origin_domain),
                    )
                    for item in stored
                )
            except ValueError as exc:
                raise RuntimeError("persisted E2EE package claim is invalid") from exc
            continue
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
        participant_claims: list[dict[str, str]] = []
        for device in devices:
            if device.id == excluded_device_id:
                continue
            if len(participant_claims) >= remaining:
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
            package.claimed_operation_id = operation_id
            package.claimed_operation_domain = operation_domain
            participant_claims.append(
                {
                    "user_id": str(participant.id),
                    "user_domain": participant.origin_domain,
                    "device_id": device.id,
                    "identity_key": encode_base64url(device.identity_key),
                    "credential": encode_base64url(device.credential),
                    "key_package": encode_base64url(package.package_data),
                }
            )
        response: dict[str, object] = {"key_packages": participant_claims}
        session.add(
            E2EEPackageClaimBatch(
                operation_id=operation_id,
                operation_domain=operation_domain,
                target_id=participant.id,
                target_domain=participant.origin_domain,
                target_is_local=True,
                channel_id=channel_ref[0],
                channel_domain=channel_ref[1],
                claimant_id=claimant_ref[0],
                claimant_domain=claimant_ref[1],
                excluded_device_id=excluded_device_id or None,
                max_devices=remaining,
                request_digest=request_digest,
                response=response,
            )
        )
        claimed.extend(participant_claims)
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
    operation_id: str,
    operation_domain: str,
    excluded_device_id: str,
) -> list[dict[str, str]]:
    local = [
        participant for participant in participants if participant.origin_domain == settings.domain
    ]
    claimed = await claim_local_room_key_packages(
        session,
        local,
        operation_id=operation_id,
        operation_domain=operation_domain,
        channel_ref=(channel.id, channel.origin_domain),
        claimant_ref=(claimant.id, claimant.origin_domain),
        excluded_device_id=excluded_device_id,
        max_devices=MAX_ROOM_E2EE_DEVICES,
    )
    # The authority operation already exists durably. Commit local claims
    # before crossing the network so a crash can resume every home with the
    # same operation ID instead of losing only one side of the claim set.
    await session.commit()
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
                    "operation_id": operation_id,
                    "operation_domain": operation_domain,
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
        event_actor = actor
        if actor.origin_domain != settings.domain:
            owner_actor = await session.get(
                User,
                (access.guild.owner_id, access.guild.owner_domain),
            )
            if owner_actor is None or not owner_actor.is_local:
                raise RuntimeError("local guild owner cannot sign the E2EE policy update")
            event_actor = owner_actor
        await queue_guild_mutation(
            session,
            settings,
            access.guild,
            event_actor,
            "guild.channel.update",
            {"channel": federation_channel_state(access.channel)},
            channel=access.channel,
        )


def _operation_request_digest(
    kind: str,
    channel: Channel,
    actor: User,
    payload: RoomProposalRequest,
) -> bytes:
    return protocol_request_digest(
        "kaede-e2ee-room-operation-v2",
        {
            "kind": kind,
            "operation_id": payload.operation_id,
            "channel_id": str(channel.id),
            "channel_domain": channel.origin_domain,
            "actor_id": str(actor.id),
            "actor_domain": actor.origin_domain,
            "sender_device_id": payload.sender_device_id,
        },
    )


def _activation_request_digest(kind: str, payload: RoomActivationRequest) -> bytes:
    return protocol_request_digest(
        "kaede-e2ee-room-activation-v2",
        {
            "kind": kind,
            "operation_id": payload.operation_id,
            "sender_device_id": payload.sender_device_id,
            "policy_generation": payload.policy_generation,
            "epoch": payload.epoch,
            "group_id": payload.group_id,
            "commit": payload.commit,
            "welcome": payload.welcome,
            "prepared_vault_revision": payload.prepared_vault_revision,
            "prepared_vault_digest": payload.prepared_vault_digest,
        },
    )


def _operation_policy(operation: E2EERoomOperation) -> dict[str, object]:
    return {
        "mode": "plaintext" if operation.kind == "activate" else "e2ee",
        "state": "proposed" if operation.kind == "activate" else "rekeying",
        "generation": str(operation.policy_generation),
        "protocol": E2EE_PROTOCOL_MLS_10,
        "suite": E2EE_SUITE_MLS_128,
        "group_id": operation.group_id,
        "epoch": None,
    }


def _operation_participant_refs(participants: list[User]) -> list[dict[str, str]]:
    return [
        {"id": str(participant.id), "domain": participant.origin_domain}
        for participant in sorted(participants, key=lambda item: (item.origin_domain, item.id))
    ]


def _stored_response(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"persisted E2EE {label} response is invalid")
    return cast(dict[str, object], value)


async def _propose_room_operation(
    kind: str,
    access: ChannelAccess,
    payload: RoomProposalRequest,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> dict[str, object]:
    access = await lock_local_channel_mutation(session, settings, access)
    await require_room_policy_authority(session, redis, settings, access, auth.user)
    if auth.user.origin_domain == settings.domain:
        await require_active_sender_device(session, auth.user, payload.sender_device_id)
    channel = access.channel
    if channel.type not in {0, 1, 2, 5}:
        raise HTTPException(status_code=400, detail={"code": "NOT_TEXT_CHANNEL"})
    request_digest = _operation_request_digest(kind, channel, auth.user, payload)
    operation = await session.scalar(
        select(E2EERoomOperation)
        .where(E2EERoomOperation.id == payload.operation_id)
        .with_for_update()
    )
    if operation is not None:
        if (
            operation.kind != kind
            or (operation.channel_id, operation.channel_domain)
            != (channel.id, channel.origin_domain)
            or (operation.actor_id, operation.actor_domain)
            != (auth.user.id, auth.user.origin_domain)
            or operation.sender_device_id != payload.sender_device_id
            or not secrets.compare_digest(operation.request_digest, request_digest)
        ):
            raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_CONFLICT"})
        if operation.status in {"prepared", "committed"}:
            return _stored_response(operation.prepared_response, "prepared")
        if operation.status == "failed" or operation.expires_at <= datetime.now(UTC):
            if operation.status != "failed":
                operation.status = "failed"
                await session.commit()
            raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_EXPIRED"})
        # A retry of a durable `claiming` operation entered with the guild or
        # DM authority mutation lock held. End that transaction before package
        # claiming takes User row locks, matching the new-operation path and
        # the User-before-room order used by activation/reset.
        await session.commit()
    else:
        if kind == "activate":
            valid_context = channel.encryption_mode == "plaintext" and channel.encryption_state in {
                "plaintext",
                "failed",
            }
        else:
            valid_context = (
                channel.encryption_mode == "e2ee" and channel.encryption_state == "active"
            )
        if not valid_context:
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_POLICY_CONTEXT_MISMATCH"},
            )
        active = await session.scalar(
            select(E2EERoomOperation)
            .where(
                E2EERoomOperation.channel_id == channel.id,
                E2EERoomOperation.channel_domain == channel.origin_domain,
                E2EERoomOperation.status.in_(("claiming", "prepared")),
            )
            .with_for_update()
        )
        if active is not None:
            if active.expires_at <= datetime.now(UTC):
                active.status = "failed"
                await session.flush()
            else:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "E2EE_OPERATION_IN_PROGRESS", "operation_id": active.id},
                )
        participants = await room_participants(session, redis, access)
        operation = E2EERoomOperation(
            id=payload.operation_id,
            authority_domain=settings.domain,
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            actor_id=auth.user.id,
            actor_domain=auth.user.origin_domain,
            sender_device_id=payload.sender_device_id,
            kind=kind,
            status="claiming",
            request_digest=request_digest,
            base_policy_generation=channel.encryption_policy_generation,
            policy_generation=channel.encryption_policy_generation + 1,
            group_id=encode_base64url(secrets.token_bytes(32)),
            participant_refs=_operation_participant_refs(participants),
            key_packages=[],
            expires_at=datetime.now(UTC) + ROOM_OPERATION_TTL,
        )
        session.add(operation)
        await session.commit()

    participants = []
    for ref in operation.participant_refs:
        participant = await session.get(User, (int(ref["id"]), ref["domain"]))
        if participant is None:
            operation.status = "failed"
            await session.commit()
            raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_STALE"})
        participants.append(participant)
    current_participants = await room_participants(session, redis, access)
    if _operation_participant_refs(current_participants) != operation.participant_refs:
        operation.status = "failed"
        await session.commit()
        raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_STALE"})
    packages = await claim_room_key_packages(
        session,
        settings,
        participants,
        claimant=auth.user,
        channel=channel,
        operation_id=operation.id,
        operation_domain=operation.authority_domain,
        excluded_device_id=payload.sender_device_id,
    )
    if not packages:
        operation = await session.get(E2EERoomOperation, payload.operation_id)
        if operation is not None:
            operation.status = "failed"
            await session.commit()
        raise HTTPException(status_code=409, detail={"code": "E2EE_NO_OTHER_DEVICES"})
    operation = await session.scalar(
        select(E2EERoomOperation)
        .where(E2EERoomOperation.id == payload.operation_id)
        .with_for_update()
    )
    if operation is None:
        raise RuntimeError("durable E2EE operation disappeared")
    if operation.status in {"prepared", "committed"}:
        return _stored_response(operation.prepared_response, "prepared")
    if operation.status != "claiming":
        raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_CONFLICT"})
    response: dict[str, object] = {
        "operation_id": operation.id,
        "status": "prepared",
        "policy": _operation_policy(operation),
        "key_packages": packages,
    }
    operation.key_packages = packages
    operation.prepared_response = response
    operation.status = "prepared"
    await session.commit()
    return response


async def _route_room_proposal(
    kind: str,
    channel_id: EntityRef,
    payload: RoomProposalRequest,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
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
            f"/_kaede/v1/e2ee/rooms/{'rekey/' if kind == 'rekey' else ''}propose",
            channel=access.channel,
            actor=auth.user,
            body=payload.model_dump(),
        )
    return await _propose_room_operation(
        kind,
        access,
        payload,
        auth,
        session,
        redis,
        settings,
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
    return await _route_room_proposal(
        "activate", channel_id, payload, auth, session, redis, settings
    )


@router.post("/channels/{channel_id}/rekey/propose")
async def propose_room_rekey(
    channel_id: EntityRef,
    payload: RoomProposalRequest,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await _route_room_proposal("rekey", channel_id, payload, auth, session, redis, settings)


async def apply_e2ee_control_metadata(
    session: AsyncSession,
    message: Message,
    value: object,
    *,
    expected_authority: str,
) -> None:
    """Persist signed control application metadata outside the MLS envelope."""

    envelope = message.e2ee if isinstance(message.e2ee, dict) else None
    operation = envelope.get("operation") if envelope is not None else None
    if operation not in {"welcome", "commit"}:
        if value is not None:
            raise ValueError("non-control message contains E2EE control metadata")
        return
    if not isinstance(value, dict) or set(value) != {
        "operation_id",
        "operation_domain",
        "apply",
    }:
        raise ValueError("E2EE control metadata is invalid")
    operation_id = value.get("operation_id")
    operation_domain = value.get("operation_domain")
    apply = value.get("apply")
    if (
        not isinstance(operation_id, str)
        or len(operation_id) != 47
        or not operation_id.startswith("keo_")
        or not all(character.isalnum() or character in "-_" for character in operation_id[4:])
        or operation_domain != expected_authority
        or not isinstance(apply, bool)
        or (operation == "welcome" and not apply)
    ):
        raise ValueError("E2EE control metadata is invalid")
    apply_mode = "join" if operation == "welcome" else "process" if apply else "audit"
    record = await session.get(E2EEControlRecord, (message.id, message.origin_domain))
    if record is None:
        record = E2EEControlRecord(
            id=message.id,
            origin_domain=message.origin_domain,
            channel_id=message.channel_id,
            channel_domain=message.channel_domain,
            author_id=message.author_id,
            author_domain=message.author_domain,
            policy_generation=message.encryption_policy_generation,
            epoch=message.encryption_epoch or 0,
            operation=str(operation),
            apply_mode=apply_mode,
            room_operation_id=operation_id,
            room_operation_domain=str(operation_domain),
            envelope=envelope,
            created_at=message.created_at,
        )
        session.add(record)
        return
    if (
        record.operation != operation
        or record.envelope != envelope
        or (
            record.room_operation_id is not None
            and (
                record.room_operation_id != operation_id
                or record.room_operation_domain != operation_domain
                or record.apply_mode != apply_mode
            )
        )
    ):
        raise ValueError("E2EE control metadata conflicts with its durable record")
    record.apply_mode = apply_mode
    record.room_operation_id = operation_id
    record.room_operation_domain = str(operation_domain)


def _control_metadata(operation: E2EERoomOperation, *, apply: bool) -> dict[str, object]:
    return {
        "operation_id": operation.id,
        "operation_domain": operation.authority_domain,
        "apply": apply,
    }


async def _commit_room_operation(
    kind: str,
    channel_id: EntityRef,
    payload: RoomActivationRequest,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    *,
    actor_home_attested: bool,
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
        await require_prepared_account_vault(
            session,
            redis,
            auth.user,
            lease_token=payload.vault_lease_token,
            revision=payload.prepared_vault_revision,
            digest=payload.prepared_vault_digest,
        )
        body = payload.model_dump(exclude={"vault_lease_token"})
        body["vault_attested"] = True
        rendered = await proxy_room_e2ee_request(
            session,
            settings,
            authority,
            f"/_kaede/v1/e2ee/rooms/{'rekey/' if kind == 'rekey' else ''}activate",
            channel=access.channel,
            actor=auth.user,
            body=body,
        )
        operation_status = await proxy_room_e2ee_request(
            session,
            settings,
            authority,
            "/_kaede/v1/e2ee/rooms/operations/status",
            channel=access.channel,
            actor=auth.user,
            body={"operation_id": payload.operation_id},
        )
        validate_remote_room_commit_response(
            rendered,
            operation_status,
            kind=kind,
            operation_id=payload.operation_id,
            channel=access.channel,
            policy_generation=payload.policy_generation,
            group_id=payload.group_id,
            authority=authority,
        )
        await apply_remote_active_policy(session, access.channel, rendered)
        return rendered

    # A local actor's durable User row is the vault-lease fencing point. Take
    # it before the channel lock and retain both locks through the operation
    # commit, so a reset/vault writer and activation have one total order.
    if auth.user.origin_domain == settings.domain:
        await require_active_sender_device(session, auth.user, payload.sender_device_id)
        await require_prepared_account_vault(
            session,
            redis,
            auth.user,
            lease_token=payload.vault_lease_token,
            revision=payload.prepared_vault_revision,
            digest=payload.prepared_vault_digest,
        )
    elif not actor_home_attested:
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_ACCOUNT_VAULT_ATTESTATION_REQUIRED"},
        )
    access = await lock_local_channel_mutation(session, settings, access)
    conversation = await require_room_policy_authority(session, redis, settings, access, auth.user)
    channel = access.channel
    operation = await session.scalar(
        select(E2EERoomOperation)
        .where(E2EERoomOperation.id == payload.operation_id)
        .with_for_update()
    )
    activation_digest = _activation_request_digest(kind, payload)
    if operation is None:
        raise HTTPException(status_code=404, detail={"code": "E2EE_OPERATION_NOT_FOUND"})
    if (
        operation.kind != kind
        or (operation.channel_id, operation.channel_domain) != (channel.id, channel.origin_domain)
        or (operation.actor_id, operation.actor_domain) != (auth.user.id, auth.user.origin_domain)
        or operation.sender_device_id != payload.sender_device_id
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_CONFLICT"})
    if operation.status == "committed":
        if operation.activation_request_digest is None or not secrets.compare_digest(
            operation.activation_request_digest, activation_digest
        ):
            raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_CONFLICT"})
        return _stored_response(operation.committed_response, "committed")
    if operation.status != "prepared" or operation.expires_at <= datetime.now(UTC):
        if operation.status == "prepared" and operation.expires_at <= datetime.now(UTC):
            operation.status = "failed"
            await session.commit()
        raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_EXPIRED"})
    if (
        operation.policy_generation != int(payload.policy_generation)
        or operation.group_id != payload.group_id
        or int(payload.epoch) != 1
    ):
        raise HTTPException(status_code=409, detail={"code": "E2EE_POLICY_CONTEXT_MISMATCH"})
    if kind == "activate":
        valid_context = (
            channel.encryption_mode == "plaintext"
            and channel.encryption_state in {"plaintext", "failed"}
            and channel.encryption_policy_generation == operation.base_policy_generation
        )
    else:
        valid_context = (
            channel.encryption_mode == "e2ee"
            and channel.encryption_state == "active"
            and channel.encryption_policy_generation == operation.base_policy_generation
        )
    if not valid_context:
        raise HTTPException(status_code=409, detail={"code": "E2EE_POLICY_CONTEXT_MISMATCH"})
    participants = await room_participants(session, redis, access)
    if _operation_participant_refs(participants) != operation.participant_refs:
        operation.status = "failed"
        await session.commit()
        raise HTTPException(status_code=409, detail={"code": "E2EE_OPERATION_STALE"})
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
    channel.encryption_protocol = E2EE_PROTOCOL_MLS_10
    channel.encryption_suite = E2EE_SUITE_MLS_128
    channel.encryption_policy_generation = operation.policy_generation
    channel.encryption_group_id = operation.group_id
    channel.encryption_epoch = 1
    if kind == "activate":
        channel.encryption_activated_at = datetime.now(UTC)
    controls: list[tuple[Message, dict[str, object]]] = []
    for control_operation, ciphertext, apply in (
        ("welcome", payload.welcome, True),
        ("commit", payload.commit, False),
    ):
        control_envelope = validate_e2ee_envelope(
            {
                "version": 2,
                "protocol": E2EE_PROTOCOL_MLS_10,
                "suite": E2EE_SUITE_MLS_128,
                "group_id": operation.group_id,
                "policy_generation": str(operation.policy_generation),
                "epoch": "1",
                "sender_device_id": payload.sender_device_id,
                "operation": control_operation,
                "ciphertext": ciphertext,
            }
        )
        if control_envelope is None:
            raise RuntimeError("validated MLS control disappeared")
        message_id = await snowflake.mint()
        if conversation is not None:
            if not controls:
                await lock_federated_dm_authority(session, conversation.authority_domain)
            await admit_federated_dm_message(
                session,
                settings,
                conversation,
                message_id=message_id,
                message_domain=settings.domain,
                delta=dm_message_storage_delta(
                    content=None,
                    e2ee=control_envelope,
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
            e2ee=control_envelope,
            encryption_policy_generation=operation.policy_generation,
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
        controls.append((message, _control_metadata(operation, apply=apply)))
    channel.last_message_id = controls[-1][0].id
    channel.last_message_domain = settings.domain
    await session.flush()
    for message, metadata in controls:
        await apply_e2ee_control_metadata(
            session,
            message,
            metadata,
            expected_authority=settings.domain,
        )
    await publish_policy_update(session, redis, settings, access, auth.user)
    rendered_messages = [message_payload(message, auth.user, []) for message, _ in controls]
    if access.guild is not None:
        event_actor = auth.user
        event_type = "guild.message.create"
        if auth.user.origin_domain != settings.domain:
            owner_actor = await session.get(
                User,
                (access.guild.owner_id, access.guild.owner_domain),
            )
            if owner_actor is None or not owner_actor.is_local:
                raise RuntimeError("local guild owner cannot sign the E2EE control")
            event_actor = owner_actor
            event_type = "guild.message.committed"
        for rendered_message, (_, metadata) in zip(rendered_messages, controls, strict=True):
            await queue_guild_mutation(
                session,
                settings,
                access.guild,
                event_actor,
                event_type,
                {
                    "message": rendered_message,
                    "author": profile_from_user(auth.user),
                    "e2ee_control": metadata,
                },
                channel=channel,
            )
    elif conversation is not None:
        event_type = (
            "dm.group.message.committed" if conversation.type == "group" else "dm.message.create"
        )
        destinations = {
            participant.origin_domain
            for participant in access.participants
            if participant.origin_domain != settings.domain
        }
        for rendered_message, (_, metadata) in zip(rendered_messages, controls, strict=True):
            event_content = {
                "message": rendered_message,
                "author": profile_from_user(auth.user),
                "encryption_policy": channel_encryption_policy_payload(channel),
                "e2ee_control": metadata,
            }
            federation_envelope = await build_envelope(
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
            for destination in destinations:
                await queue_event(session, settings, destination, federation_envelope)
    rendered_channel = channel_payload(channel)
    committed_response: dict[str, object] = {
        **rendered_channel,
        "operation_id": operation.id,
        "operation_status": "committed",
        "controls": [
            {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "operation": cast(dict[str, object], message.e2ee)["operation"],
                "apply": metadata["apply"],
            }
            for message, metadata in controls
        ],
    }
    operation.activation_request_digest = activation_digest
    operation.prepared_vault_revision = int(payload.prepared_vault_revision)
    operation.prepared_vault_digest = decode_base64url(payload.prepared_vault_digest, size=32)
    operation.committed_response = committed_response
    operation.status = "committed"
    operation.committed_at = datetime.now(UTC)
    await session.commit()
    if access.guild is not None:
        await wake_queued_guild_federation(access.guild)
    await publish_channel_dispatch(redis, access, "CHANNEL_UPDATE", rendered_channel)
    for rendered_message in rendered_messages:
        await publish_channel_dispatch(redis, access, "MESSAGE_CREATE", rendered_message)
    return committed_response


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
    return await _commit_room_operation(
        "activate",
        channel_id,
        payload,
        auth,
        session,
        redis,
        snowflake,
        settings,
        actor_home_attested=False,
    )


async def activate_room_encryption_attested(
    channel_id: EntityRef,
    payload: RoomActivationRequest,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> dict[str, object]:
    return await _commit_room_operation(
        "activate",
        channel_id,
        payload,
        auth,
        session,
        redis,
        snowflake,
        settings,
        actor_home_attested=True,
    )


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
    return await _commit_room_operation(
        "rekey",
        channel_id,
        payload,
        auth,
        session,
        redis,
        snowflake,
        settings,
        actor_home_attested=False,
    )


async def activate_room_rekey_attested(
    channel_id: EntityRef,
    payload: RoomRekeyActivationRequest,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> dict[str, object]:
    return await _commit_room_operation(
        "rekey",
        channel_id,
        payload,
        auth,
        session,
        redis,
        snowflake,
        settings,
        actor_home_attested=True,
    )


async def room_encryption_operation_status_for_actor(
    channel_id: EntityRef,
    operation_id: str,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
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
        return await proxy_room_e2ee_request(
            session,
            settings,
            authority,
            "/_kaede/v1/e2ee/rooms/operations/status",
            channel=access.channel,
            actor=auth.user,
            body={"operation_id": operation_id},
        )
    operation = await session.get(E2EERoomOperation, operation_id)
    if (
        operation is None
        or (operation.channel_id, operation.channel_domain)
        != (access.channel.id, access.channel.origin_domain)
        or (operation.actor_id, operation.actor_domain) != (auth.user.id, auth.user.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "E2EE_OPERATION_NOT_FOUND"})
    return {
        "operation_id": operation.id,
        "kind": operation.kind,
        "status": operation.status,
        "prepared": operation.prepared_response,
        "committed": operation.committed_response,
        "expires_at": operation.expires_at.isoformat(),
        "committed_at": operation.committed_at.isoformat()
        if operation.committed_at is not None
        else None,
    }


@router.get("/channels/{channel_id}/operations/{operation_id}")
async def room_encryption_operation_status(
    channel_id: EntityRef,
    operation_id: str = Path(pattern=r"^keo_[A-Za-z0-9_-]{43}$"),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await room_encryption_operation_status_for_actor(
        channel_id,
        operation_id,
        auth,
        session,
        redis,
        settings,
    )
