from __future__ import annotations

import secrets
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import HTTPException
from pydantic import ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import delete, exists, false, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.chat.automation_e2ee import (
    automation_device_protocol_id,
    automation_device_registration_input,
    automation_key_package_upload_input,
    automation_mls_credential,
)
from app.chat.e2ee import E2EE_SUITE_MLS_128
from app.chat.postcommit import queue_postcommit_federation_wakes
from app.core.base64url import decode_base64url, encode_base64url
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import DOMAIN_RE, Settings
from app.core.snowflake import SnowflakeGenerator
from app.db.bot_models import (
    BotApplication,
    BotDMCapability,
    BotDMGrant,
    BotDMGrantConsent,
    BotE2EEDevice,
    BotE2EEKeyPackage,
    BotE2EEParticipation,
    BotInstallation,
    BotUserInstallation,
    BotWorker,
)
from app.db.models import Channel, DMConversation, DMParticipant, User
from app.federation.network import normalize_domain
from app.federation.schemas import EventEnvelope
from app.federation.security import validated_event_envelope
from app.voice.e2ee import (
    MediaSessionRotationError,
    evict_bot_voice_runtime_sessions,
    evict_channel_media_sessions,
)

BOT_E2EE_DEVICE_SNAPSHOT_EVENT = "bot.e2ee.device-snapshot"
BOT_E2EE_CAPABILITIES = frozenset({"e2ee-mls/1", "e2ee-media/1"})
MAX_BOT_E2EE_DEVICES = 16
MAX_BOT_E2EE_KEY_PACKAGES_PER_DEVICE = 100
MAX_BOT_E2EE_KEY_PACKAGE_BYTES = 32 * 1024
MAX_BOT_E2EE_CREDENTIAL_BYTES = 16 * 1024
MAX_BOT_E2EE_DEVICE_SNAPSHOT_EVENT = 512 * 1024
BotRuntimeInstallation = BotInstallation | BotUserInstallation | BotDMCapability


async def revoke_bot_e2ee_access(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    installation_ids: Iterable[int] = (),
    user_installation_ids: Iterable[int] = (),
    dm_capability_ids: Iterable[int] = (),
    device_ids: Iterable[int] = (),
    application_ref: tuple[int, str] | None = None,
    worker_ids: Iterable[int] = (),
    now: datetime | None = None,
) -> list[Channel]:
    """Atomically revoke bot MLS grants and pause every affected room.

    Callers hold the installation, worker, device, or application authority row
    that defines the revocation. This helper performs no commit so that access,
    room policy, and the caller's lifecycle mutation have one transaction.
    """

    direct_installation_ids = set(installation_ids)
    direct_user_installation_ids = set(user_installation_ids)
    direct_dm_capability_ids = set(dm_capability_ids)
    direct_device_ids = set(device_ids)
    direct_worker_ids = set(worker_ids)
    if not any(
        (
            direct_installation_ids,
            direct_user_installation_ids,
            direct_dm_capability_ids,
            direct_device_ids,
            application_ref,
            direct_worker_ids,
        )
    ):
        return []

    from app.bots.dm_capability import (
        fence_bot_dm_capability_projections,
        revoke_bot_dm_capabilities,
    )

    revoked_capabilities, capability_destinations = await revoke_bot_dm_capabilities(
        session,
        settings,
        guild_installation_ids=direct_installation_ids,
        user_installation_ids=direct_user_installation_ids,
        application_ref=application_ref,
        now=now,
    )
    direct_dm_capability_ids.update(item.id for item in revoked_capabilities)
    queue_postcommit_federation_wakes(session, sorted(capability_destinations))
    if application_ref is not None:
        fenced_capabilities = await fence_bot_dm_capability_projections(
            session,
            application_ref=application_ref,
            now=now,
        )
        direct_dm_capability_ids.update(item.id for item in fenced_capabilities)
    runtime_capabilities = (
        list(
            await session.scalars(
                select(BotDMCapability)
                .where(BotDMCapability.id.in_(direct_dm_capability_ids))
                .order_by(BotDMCapability.id)
                .with_for_update()
            )
        )
        if direct_dm_capability_ids
        else []
    )
    try:
        await evict_bot_voice_runtime_sessions(
            session,
            redis,
            settings,
            application_ref=application_ref,
            worker_ids=direct_worker_ids,
            installation_ids=direct_installation_ids,
            capability_grant_ids=(item.grant_id for item in runtime_capabilities),
        )
    except MediaSessionRotationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "E2EE_MEDIA_ROTATION_UNAVAILABLE", "retry_after_ms": 2000},
            headers={"Retry-After": "2"},
        ) from exc

    grant_conditions: list[ColumnElement[bool]] = []
    if direct_installation_ids:
        grant_conditions.append(BotDMGrant.installation_id.in_(direct_installation_ids))
    if direct_user_installation_ids:
        grant_conditions.append(BotDMGrant.user_installation_id.in_(direct_user_installation_ids))
    if direct_dm_capability_ids:
        grant_conditions.append(BotDMGrant.dm_capability_id.in_(direct_dm_capability_ids))
    if application_ref is not None:
        grant_conditions.append(
            (BotDMGrant.application_id == application_ref[0])
            & (BotDMGrant.application_domain == application_ref[1])
        )
    grants: list[BotDMGrant] = []
    if grant_conditions:
        grants = list(
            await session.scalars(
                select(BotDMGrant)
                .where(
                    or_(*grant_conditions),
                    BotDMGrant.consent_state.in_(("pending", "active")),
                    BotDMGrant.revoked_at.is_(None),
                )
                .order_by(BotDMGrant.id)
                .with_for_update()
            )
        )

    device_conditions: list[ColumnElement[bool]] = []
    if direct_device_ids:
        device_conditions.append(BotE2EEDevice.id.in_(direct_device_ids))
    if application_ref is not None:
        application_devices = (BotE2EEDevice.application_id == application_ref[0]) & (
            BotE2EEDevice.application_domain == application_ref[1]
        )
        if direct_worker_ids:
            application_devices &= BotE2EEDevice.worker_id.in_(direct_worker_ids)
        device_conditions.append(application_devices)
    elif direct_worker_ids:
        raise ValueError("worker revocation requires an application authority")

    participation_conditions: list[ColumnElement[bool]] = []
    if direct_installation_ids:
        participation_conditions.append(
            BotE2EEParticipation.installation_id.in_(direct_installation_ids)
        )
    if grants:
        participation_conditions.append(
            BotE2EEParticipation.dm_grant_id.in_([grant.id for grant in grants])
        )
    if device_conditions:
        participation_conditions.append(
            BotE2EEParticipation.device_id.in_(
                select(BotE2EEDevice.id).where(or_(*device_conditions))
            )
        )
    if not participation_conditions:
        return []

    participation_query = select(BotE2EEParticipation).where(
        or_(*participation_conditions),
        BotE2EEParticipation.status.in_(("pending", "active")),
    )
    candidate_rows = list(await session.scalars(participation_query))
    channel_refs = {(row.channel_id, row.channel_domain) for row in candidate_rows}
    channels = (
        list(
            await session.scalars(
                select(Channel)
                .where(tuple_(Channel.id, Channel.origin_domain).in_(channel_refs))
                .order_by(Channel.origin_domain, Channel.id)
                .with_for_update()
            )
        )
        if channel_refs
        else []
    )
    participations = list(
        await session.scalars(
            participation_query.order_by(BotE2EEParticipation.id).with_for_update()
        )
    )
    if {(row.channel_id, row.channel_domain) for row in participations} - channel_refs:
        raise RuntimeError("bot E2EE participation changed during revocation")

    for channel in channels:
        if channel.encryption_mode != "e2ee" or channel.encryption_state != "active":
            continue
        conversation = (
            await session.get(DMConversation, (channel.id, channel.origin_domain))
            if channel.guild_id is None
            else None
        )
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

    revoked_at = now or datetime.now(UTC)
    for grant in grants:
        grant.consent_state = "revoked"
        grant.revoked_at = revoked_at
        grant.consent_generation += 1
    for participation in participations:
        participation.status = "revoked"
        participation.revoked_at = revoked_at
        participation.consent_generation += 1
    for channel in channels:
        if channel.encryption_mode == "e2ee" and channel.encryption_state == "active":
            channel.encryption_state = "rekeying"
    return channels


async def revoke_bot_e2ee_devices(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    application_ref: tuple[int, str],
    worker_ids: Iterable[int] = (),
    device_ids: Iterable[int] = (),
    now: datetime | None = None,
) -> tuple[User | None, list[Channel]]:
    """Revoke trusted worker devices and their room access in one transaction."""

    worker_id_set = set(worker_ids)
    device_id_set = set(device_ids)
    conditions = [
        BotE2EEDevice.application_id == application_ref[0],
        BotE2EEDevice.application_domain == application_ref[1],
        BotE2EEDevice.trust_state == "trusted",
        BotE2EEDevice.revoked_at.is_(None),
    ]
    if worker_id_set:
        conditions.append(BotE2EEDevice.worker_id.in_(worker_id_set))
    if device_id_set:
        conditions.append(BotE2EEDevice.id.in_(device_id_set))
    devices = list(
        await session.scalars(
            select(BotE2EEDevice).where(*conditions).order_by(BotE2EEDevice.id).with_for_update()
        )
    )
    if not devices:
        return None, []
    revoked_at = now or datetime.now(UTC)
    try:
        await evict_bot_voice_runtime_sessions(
            session,
            redis,
            settings,
            application_ref=application_ref,
            worker_ids=worker_id_set,
            device_protocol_ids=(device.protocol_id for device in devices),
        )
    except MediaSessionRotationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "E2EE_MEDIA_ROTATION_UNAVAILABLE", "retry_after_ms": 2000},
            headers={"Retry-After": "2"},
        ) from exc
    channels = await revoke_bot_e2ee_access(
        session,
        redis,
        settings,
        device_ids=[device.id for device in devices],
        now=revoked_at,
    )
    await session.execute(
        delete(BotE2EEKeyPackage).where(
            BotE2EEKeyPackage.device_id.in_([device.id for device in devices]),
            BotE2EEKeyPackage.claimed_at.is_(None),
        )
    )
    for device in devices:
        device.trust_state = "revoked"
        device.revoked_at = revoked_at
    bot = await session.scalar(
        select(User)
        .join(
            BotApplication,
            (BotApplication.bot_user_id == User.id)
            & (BotApplication.bot_user_domain == User.origin_domain),
        )
        .where(
            BotApplication.id == application_ref[0],
            BotApplication.origin_domain == application_ref[1],
            User.account_type == "bot",
        )
        .with_for_update(of=User)
    )
    if bot is None:
        raise RuntimeError("bot E2EE device lost its application identity")
    bot.e2ee_device_generation = max(0, int(bot.e2ee_device_generation or 0)) + 1
    return bot, channels


def bot_device_protocol_id(
    application_id: int,
    application_domain: str,
    worker_id: int,
    identity_key: bytes,
) -> str:
    """Return the federation-stable MLS device ID for one worker identity."""

    return automation_device_protocol_id(
        namespace="kaede-bot-e2ee-device-v1",
        prefix="kbe_",
        principal_ref=f"{application_id}@{application_domain}\0{worker_id}",
        identity_key=identity_key,
    )


def bot_mls_credential(
    application_id: int,
    application_domain: str,
    worker_id: int,
    protocol_id: str,
) -> bytes:
    """Return the only credential accepted for a newly registered bot device."""

    application_ref = f"{application_id}@{application_domain}"
    return automation_mls_credential(
        account=f"bot:{application_ref}:worker:{worker_id}",
        credential_type="kaede-bot-device-v2",
        device_id=protocol_id,
        lineage={"application_ref": application_ref, "worker_id": str(worker_id)},
    )


def bot_device_registration_input(
    *,
    application_id: int,
    application_domain: str,
    worker_id: int,
    identity_key: bytes,
    credential_digest: bytes,
    challenge: bytes,
) -> bytes:
    return automation_device_registration_input(
        namespace="kaede-bot-e2ee-device-registration-v1",
        principal_ref=f"{application_id}@{application_domain}",
        lineage=(str(worker_id),),
        identity_key=identity_key,
        credential_digest=credential_digest,
        challenge=challenge,
    )


def bot_key_package_upload_input(
    *,
    protocol_id: str,
    generation: int,
    cipher_suite: str,
    expires_at: datetime,
    package_hashes: Iterable[bytes],
) -> bytes:
    return automation_key_package_upload_input(
        namespace="kaede-bot-e2ee-key-packages-v1",
        protocol_id=protocol_id,
        generation=generation,
        cipher_suite=cipher_suite,
        expires_at=expires_at,
        package_hashes=package_hashes,
    )


class BotE2EEDeviceDescriptor(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    source_domain: str = Field(min_length=1, max_length=253)
    protocol_id: str = Field(pattern=r"^kbe_[A-Za-z0-9_-]{43}$")
    worker_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    identity_key: str = Field(min_length=43, max_length=43)
    credential: str = Field(min_length=2, max_length=22_000)
    capabilities: list[str] = Field(min_length=1, max_length=8)
    generation: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    trust_state: Literal["trusted"] = "trusted"

    @field_validator("source_domain")
    @classmethod
    def canonical_domain(cls, value: str) -> str:
        if value != value.rstrip(".").lower() or DOMAIN_RE.fullmatch(value) is None:
            raise ValueError("device authority domain must be canonical")
        return value

    @field_validator("identity_key")
    @classmethod
    def valid_identity_key(cls, value: str) -> str:
        decode_base64url(value, size=32)
        return value

    @field_validator("credential")
    @classmethod
    def valid_credential(cls, value: str) -> str:
        decode_base64url(value, maximum=MAX_BOT_E2EE_CREDENTIAL_BYTES)
        return value

    @field_validator("capabilities")
    @classmethod
    def valid_capabilities(cls, value: list[str]) -> list[str]:
        if (
            len(value) != len(set(value))
            or not set(value) <= BOT_E2EE_CAPABILITIES
            or "e2ee-mls/1" not in value
        ):
            raise ValueError("bot E2EE capabilities are invalid")
        return sorted(value)


class BotE2EEDeviceSnapshot(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    application_domain: str = Field(min_length=1, max_length=253)
    bot_user_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    bot_user_domain: str = Field(min_length=1, max_length=253)
    generation: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    devices: list[BotE2EEDeviceDescriptor] = Field(max_length=MAX_BOT_E2EE_DEVICES)

    @model_validator(mode="after")
    def coherent_authority(self) -> BotE2EEDeviceSnapshot:
        domains = {self.application_domain, self.bot_user_domain}
        domains.update(device.source_domain for device in self.devices)
        if len(domains) != 1:
            raise ValueError("bot E2EE device snapshot authority is inconsistent")
        if len({device.protocol_id for device in self.devices}) != len(self.devices):
            raise ValueError("bot E2EE device snapshot contains duplicate devices")
        return self


def render_bot_e2ee_device(device: BotE2EEDevice, worker: BotWorker) -> dict[str, object]:
    return {
        "source_id": str(device.source_id if device.source_id is not None else device.id),
        "source_domain": device.source_domain or device.application_domain,
        "protocol_id": device.protocol_id,
        "worker_id": str(worker.authority_id),
        "identity_key": encode_base64url(device.identity_key),
        "credential": encode_base64url(device.credential),
        "capabilities": list(device.capabilities or []),
        "generation": str(device.generation),
        "trust_state": "trusted",
    }


async def local_bot_e2ee_snapshot(
    session: AsyncSession,
    application: BotApplication,
    bot: User,
) -> BotE2EEDeviceSnapshot:
    rows = list(
        (
            await session.execute(
                select(BotE2EEDevice, BotWorker)
                .join(BotWorker, BotWorker.id == BotE2EEDevice.worker_id)
                .where(
                    BotE2EEDevice.application_id == application.id,
                    BotE2EEDevice.application_domain == application.origin_domain,
                    BotE2EEDevice.trust_state == "trusted",
                    BotE2EEDevice.revoked_at.is_(None),
                    BotWorker.revoked_at.is_(None),
                    (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > datetime.now(UTC)),
                )
                .order_by(BotE2EEDevice.protocol_id)
                .limit(MAX_BOT_E2EE_DEVICES + 1)
            )
        ).tuples()
    )
    if len(rows) > MAX_BOT_E2EE_DEVICES:
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_DEVICE_LIMIT"})
    generation = max(1, int(bot.e2ee_device_generation or 0))
    return BotE2EEDeviceSnapshot.model_validate(
        {
            "application_id": str(application.id),
            "application_domain": application.origin_domain,
            "bot_user_id": str(bot.id),
            "bot_user_domain": bot.origin_domain,
            "generation": str(generation),
            "devices": [render_bot_e2ee_device(device, worker) for device, worker in rows],
        }
    )


async def validated_bot_e2ee_snapshot(
    session: AsyncSession,
    settings: Settings,
    application_domain: str,
    raw: object,
    *,
    application_id: int,
    bot_user_ref: tuple[int, str],
) -> BotE2EEDeviceSnapshot:
    envelope: EventEnvelope = await validated_event_envelope(
        session,
        settings,
        application_domain,
        raw,
    )
    if envelope.type != BOT_E2EE_DEVICE_SNAPSHOT_EVENT:
        raise ValueError("bot E2EE device snapshot has the wrong type")
    snapshot = BotE2EEDeviceSnapshot.model_validate(envelope.content)
    if (
        int(snapshot.application_id) != application_id
        or snapshot.application_domain != application_domain
        or (int(snapshot.bot_user_id), snapshot.bot_user_domain) != bot_user_ref
        or (int(envelope.actor.id), envelope.actor.domain) != bot_user_ref
    ):
        raise ValueError("bot E2EE device snapshot identity is inconsistent")
    return snapshot


async def materialize_bot_e2ee_snapshot(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    application: BotApplication,
    snapshot: BotE2EEDeviceSnapshot,
    *,
    known_generation: int | None = None,
) -> list[BotE2EEDevice]:
    if (
        int(snapshot.application_id),
        snapshot.application_domain,
    ) != (application.id, application.origin_domain):
        raise ValueError("bot E2EE snapshot belongs to another application")
    existing_rows = list(
        (
            await session.execute(
                select(BotE2EEDevice, BotWorker)
                .join(BotWorker, BotWorker.id == BotE2EEDevice.worker_id)
                .where(
                    BotE2EEDevice.application_id == application.id,
                    BotE2EEDevice.application_domain == application.origin_domain,
                    BotE2EEDevice.source_domain == application.origin_domain,
                )
            )
        ).tuples()
    )
    if (
        known_generation is not None
        and int(snapshot.generation) == known_generation
        and existing_rows
    ):
        active_devices = {
            device.protocol_id: BotE2EEDeviceDescriptor.model_validate(
                render_bot_e2ee_device(device, worker)
            ).model_dump(mode="json")
            for device, worker in existing_rows
            if device.trust_state == "trusted" and device.revoked_at is None
        }
        incoming_devices = {
            descriptor.protocol_id: descriptor.model_dump(mode="json")
            for descriptor in snapshot.devices
        }
        if active_devices != incoming_devices:
            raise ValueError("bot E2EE device snapshot generation was equivocated")
        return [
            device
            for descriptor in snapshot.devices
            for device, _worker in existing_rows
            if device.protocol_id == descriptor.protocol_id
        ]

    accepted: list[BotE2EEDevice] = []
    current_protocol_ids = {descriptor.protocol_id for descriptor in snapshot.devices}
    for descriptor in snapshot.devices:
        worker = await session.scalar(
            select(BotWorker).where(
                BotWorker.application_id == application.id,
                BotWorker.application_domain == application.origin_domain,
                (
                    (
                        (BotWorker.source_id == int(descriptor.worker_id))
                        & (BotWorker.source_domain == descriptor.source_domain)
                    )
                    | (
                        (BotWorker.id == int(descriptor.worker_id))
                        & (BotWorker.source_id.is_(None))
                        & (application.origin_domain == descriptor.source_domain)
                    )
                ),
            )
        )
        if worker is None:
            raise ValueError("bot E2EE snapshot references an unknown worker")
        device = await session.scalar(
            select(BotE2EEDevice).where(
                BotE2EEDevice.source_id == int(descriptor.source_id),
                BotE2EEDevice.source_domain == descriptor.source_domain,
            )
        )
        identity_key = decode_base64url(descriptor.identity_key, size=32)
        credential = decode_base64url(
            descriptor.credential,
            maximum=MAX_BOT_E2EE_CREDENTIAL_BYTES,
        )
        if device is None:
            conflicting = await session.scalar(
                select(BotE2EEDevice).where(BotE2EEDevice.protocol_id == descriptor.protocol_id)
            )
            if conflicting is not None:
                raise ValueError("bot E2EE protocol identity collides with another device")
            device = BotE2EEDevice(
                id=await snowflake.mint(),
                source_id=int(descriptor.source_id),
                source_domain=descriptor.source_domain,
                protocol_id=descriptor.protocol_id,
                application_id=application.id,
                application_domain=application.origin_domain,
                worker_id=worker.id,
                identity_key=identity_key,
                credential=credential,
                capabilities=list(descriptor.capabilities),
                generation=int(descriptor.generation),
                trust_state="trusted",
            )
            session.add(device)
        else:
            if (
                device.application_id,
                device.application_domain,
                device.protocol_id,
            ) != (
                application.id,
                application.origin_domain,
                descriptor.protocol_id,
            ):
                raise ValueError("bot E2EE device source identity is inconsistent")
            incoming_generation = int(descriptor.generation)
            if incoming_generation < device.generation:
                raise ValueError("bot E2EE device snapshot generation regressed")
            if incoming_generation == device.generation and (
                device.worker_id != worker.id
                or not secrets.compare_digest(device.identity_key, identity_key)
                or not secrets.compare_digest(device.credential, credential)
                or set(device.capabilities or []) != set(descriptor.capabilities)
            ):
                raise ValueError("bot E2EE device generation was equivocated")
            device.worker_id = worker.id
            device.identity_key = identity_key
            device.credential = credential
            device.capabilities = list(descriptor.capabilities)
            device.generation = incoming_generation
            device.trust_state = "trusted"
            device.revoked_at = None
        accepted.append(device)
    stale_devices = list(
        await session.scalars(
            select(BotE2EEDevice).where(
                BotE2EEDevice.application_id == application.id,
                BotE2EEDevice.application_domain == application.origin_domain,
                BotE2EEDevice.source_domain == application.origin_domain,
                BotE2EEDevice.protocol_id.not_in(current_protocol_ids),
                BotE2EEDevice.revoked_at.is_(None),
            )
        )
    )
    for device in stale_devices:
        device.trust_state = "revoked"
        device.revoked_at = datetime.now(UTC)
    await session.flush()
    return accepted


async def claim_bot_e2ee_key_packages(
    session: AsyncSession,
    *,
    application: BotApplication,
    target: User,
    protocol_ids: Iterable[str],
    operation_id: str,
    operation_domain: str,
) -> list[dict[str, str]]:
    requested = list(dict.fromkeys(protocol_ids))
    if not requested or len(requested) > MAX_BOT_E2EE_DEVICES:
        raise HTTPException(status_code=409, detail={"code": "E2EE_PARTICIPANT_DEVICE_MISSING"})
    now = datetime.now(UTC)
    devices = list(
        await session.scalars(
            select(BotE2EEDevice)
            .join(BotWorker, BotWorker.id == BotE2EEDevice.worker_id)
            .where(
                BotE2EEDevice.application_id == application.id,
                BotE2EEDevice.application_domain == application.origin_domain,
                BotE2EEDevice.protocol_id.in_(requested),
                BotE2EEDevice.trust_state == "trusted",
                BotE2EEDevice.revoked_at.is_(None),
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > now),
            )
            .order_by(BotE2EEDevice.protocol_id)
        )
    )
    if {device.protocol_id for device in devices} != set(requested):
        raise HTTPException(status_code=409, detail={"code": "E2EE_PARTICIPANT_DEVICE_MISSING"})
    claimed: list[dict[str, str]] = []
    for device in devices:
        package = await session.scalar(
            select(BotE2EEKeyPackage)
            .where(
                BotE2EEKeyPackage.device_id == device.id,
                BotE2EEKeyPackage.claimed_at.is_(None),
                BotE2EEKeyPackage.expires_at > now,
                BotE2EEKeyPackage.cipher_suite == E2EE_SUITE_MLS_128,
            )
            .order_by(BotE2EEKeyPackage.expires_at, BotE2EEKeyPackage.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if package is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "E2EE_KEY_PACKAGE_UNAVAILABLE", "device_id": device.protocol_id},
            )
        package.claimed_at = now
        package.claimed_for_ref = f"{operation_id}@{normalize_domain(operation_domain)}"
        claimed.append(
            {
                "user_id": str(target.id),
                "user_domain": target.origin_domain,
                "device_id": device.protocol_id,
                "identity_key": encode_base64url(device.identity_key),
                "credential": encode_base64url(device.credential),
                "key_package": encode_base64url(package.package),
            }
        )
    return claimed


async def active_bot_e2ee_participation(
    session: AsyncSession,
    installation: BotRuntimeInstallation,
    channel: Channel,
    protocol_id: str | None,
    *,
    include_pending: bool = False,
    worker_id: int | None = None,
) -> tuple[BotE2EEParticipation, BotE2EEDevice] | None:
    statuses = ("active", "pending") if include_pending else ("active",)
    if isinstance(installation, BotUserInstallation):
        grant_installation = BotDMGrant.user_installation_id == installation.id
    elif isinstance(installation, BotDMCapability):
        grant_installation = BotDMGrant.dm_capability_id == installation.id
    else:
        grant_installation = BotDMGrant.installation_id == installation.id
    missing_participant_consent = exists(
        select(DMParticipant.user_id)
        .join(
            User,
            (User.id == DMParticipant.user_id) & (User.origin_domain == DMParticipant.user_domain),
        )
        .where(
            DMParticipant.conversation_id == BotDMGrant.conversation_id,
            DMParticipant.conversation_domain == BotDMGrant.conversation_domain,
            User.account_type != "bot",
            ~exists(
                select(BotDMGrantConsent.grant_id).where(
                    BotDMGrantConsent.grant_id == BotDMGrant.id,
                    BotDMGrantConsent.user_id == DMParticipant.user_id,
                    BotDMGrantConsent.user_domain == DMParticipant.user_domain,
                    BotDMGrantConsent.consent_generation == BotDMGrant.consent_generation,
                    BotDMGrantConsent.status == "active",
                    BotDMGrantConsent.revoked_at.is_(None),
                )
            ),
        )
    )
    eligible_dm_grants = select(BotDMGrant.id).where(
        grant_installation,
        BotDMGrant.conversation_id == channel.id,
        BotDMGrant.conversation_domain == channel.origin_domain,
        BotDMGrant.application_id == installation.application_id,
        BotDMGrant.application_domain == installation.application_domain,
        BotDMGrant.consent_state == "active",
        BotDMGrant.revoked_at.is_(None),
        ~missing_participant_consent,
    )
    direct_installation = (
        BotE2EEParticipation.installation_id == installation.id
        if isinstance(installation, BotInstallation)
        else false()
    )
    statement = (
        select(BotE2EEParticipation, BotE2EEDevice)
        .join(BotE2EEDevice, BotE2EEDevice.id == BotE2EEParticipation.device_id)
        .where(
            (direct_installation | (BotE2EEParticipation.dm_grant_id.in_(eligible_dm_grants))),
            BotE2EEParticipation.channel_id == channel.id,
            BotE2EEParticipation.channel_domain == channel.origin_domain,
            BotE2EEParticipation.status.in_(statuses),
            BotE2EEDevice.trust_state == "trusted",
            BotE2EEDevice.revoked_at.is_(None),
        )
    )
    if protocol_id is not None:
        statement = statement.where(BotE2EEDevice.protocol_id == protocol_id)
    if worker_id is not None:
        statement = statement.where(BotE2EEDevice.worker_id == worker_id)
    statement = statement.limit(1)
    row = (await session.execute(statement)).one_or_none()
    return cast(tuple[BotE2EEParticipation, BotE2EEDevice] | None, row)


async def has_active_bot_e2ee_participation(
    session: AsyncSession,
    installation: BotRuntimeInstallation,
    channel: Channel,
) -> bool:
    """Return whether any trusted device can receive this room's MLS traffic."""

    if (
        isinstance(installation, BotInstallation) and installation.e2ee_mode != "participant"
    ) or channel.encryption_state != "active":
        return False
    return await active_bot_e2ee_participation(session, installation, channel, None) is not None


async def require_bot_e2ee_worker_participation(
    session: AsyncSession,
    installation: BotRuntimeInstallation,
    channel: Channel,
    worker_id: int,
) -> tuple[BotE2EEParticipation, BotE2EEDevice]:
    """Fence interaction tokens to the worker's own active MLS device."""

    if (
        isinstance(installation, BotInstallation) and installation.e2ee_mode != "participant"
    ) or channel.encryption_state != "active":
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"})
    row = await active_bot_e2ee_participation(
        session,
        installation,
        channel,
        None,
        worker_id=worker_id,
    )
    if row is None:
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"})
    return row


async def require_bot_e2ee_participation(
    session: AsyncSession,
    installation: BotRuntimeInstallation,
    channel: Channel,
    protocol_id: str | None,
    *,
    worker_id: int | None = None,
) -> tuple[BotE2EEParticipation, BotE2EEDevice]:
    if (
        isinstance(installation, BotInstallation) and installation.e2ee_mode != "participant"
    ) or protocol_id is None:
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"})
    row = await active_bot_e2ee_participation(
        session,
        installation,
        channel,
        protocol_id,
        worker_id=worker_id,
    )
    if row is None:
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"})
    if channel.encryption_state != "active":
        raise HTTPException(status_code=409, detail={"code": "E2EE_REKEY_REQUIRED"})
    return row
