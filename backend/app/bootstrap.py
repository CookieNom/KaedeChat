from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import exists, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import (
    GuildMediaDeletionRequest,
    Instance,
    MediaTombstoneSource,
    PeerKey,
    TerminalRoomDeletion,
)
from app.db.partitions import ensure_message_partitions


class DomainMismatchError(RuntimeError):
    pass


class IdentityKeyError(RuntimeError):
    pass


class KeyRetirementTooEarly(IdentityKeyError):
    def __init__(self, key_id: str, retire_after: datetime) -> None:
        self.key_id = key_id
        self.retire_after = retire_after
        super().__init__(
            f"signing key {key_id!r} cannot retire safely before "
            f"{retire_after.astimezone(UTC).isoformat()}"
        )


# Bootstrap can be invoked by multiple one-shot containers during an operator
# retry.  The partial unique index on instances.is_self protects the final
# invariant, but without a database lock both callers can generate different
# keypairs and one will fail with an opaque integrity error.  A transaction
# advisory lock makes the check-and-create operation deterministic across
# processes and hosts that share this database.
BOOTSTRAP_ADVISORY_LOCK_ID = 5_421_990_727_754_268_272
MAX_ADVERTISED_OLD_KEYS = 64
# Outbox delivery is attempted for seven days. Source envelopes intentionally
# remain available for one additional day so the expiry sweep can issue a
# signed resynchronization marker. A historical signing key must overlap both
# that window and the operator-configured federation event retention window.
MINIMUM_SIGNING_KEY_OVERLAP = timedelta(days=8)


def signing_key_retirement_overlap(settings: Settings) -> timedelta:
    return max(
        timedelta(days=settings.federation_event_retention_days),
        MINIMUM_SIGNING_KEY_OVERLAP,
    )


def signing_key_operation_time(now: datetime | None = None) -> datetime:
    if now is not None and now.utcoffset() is None:
        raise ValueError("signing key timestamp must be timezone-aware")
    return datetime.now(UTC) if now is None else now.astimezone(UTC)


def new_signing_key_id(now: datetime | None = None) -> str:
    """Return a readable key ID that remains unique across same-day rotations."""

    generated_at = signing_key_operation_time(now)
    return f"ed25519:{generated_at.strftime('%Y%m%d')}-{secrets.token_hex(16)}"


async def signing_key_has_pending_deletion_proofs(
    session: AsyncSession,
    settings: Settings,
    key_id: str,
) -> bool:
    """Keep verification keys advertised while durable deletion still uses them."""

    return bool(
        await session.scalar(
            select(
                or_(
                    exists().where(
                        MediaTombstoneSource.attachment_domain == settings.domain,
                        MediaTombstoneSource.key_id == key_id,
                    ),
                    exists().where(
                        TerminalRoomDeletion.room_domain == settings.domain,
                        TerminalRoomDeletion.key_id == key_id,
                        TerminalRoomDeletion.acknowledged_at.is_(None),
                    ),
                    exists().where(
                        GuildMediaDeletionRequest.guild_domain == settings.domain,
                        GuildMediaDeletionRequest.key_id == key_id,
                        GuildMediaDeletionRequest.acknowledged_at.is_(None),
                    ),
                )
            )
        )
    )


async def verify_stored_identity(
    session: AsyncSession, settings: Settings, instance: Instance
) -> None:
    if (
        instance.current_key_id is None
        or instance.encrypted_private_key is None
        or instance.private_key_nonce is None
    ):
        raise IdentityKeyError("stored instance identity is missing key material")
    try:
        private_bytes = AESGCM(settings.secret_key_bytes).decrypt(
            instance.private_key_nonce,
            instance.encrypted_private_key,
            instance.domain.encode("ascii"),
        )
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    except (InvalidTag, ValueError) as exc:
        raise IdentityKeyError(
            "stored instance identity cannot be opened with KAEDE_SECRET_KEY"
        ) from exc

    stored_public_key = await session.get(PeerKey, (instance.domain, instance.current_key_id))
    derived_public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    if (
        stored_public_key is None
        or stored_public_key.expired_at is not None
        or not secrets.compare_digest(stored_public_key.public_key, derived_public_key)
    ):
        raise IdentityKeyError("stored instance identity keypair is inconsistent")


async def bootstrap_instance(session: AsyncSession, settings: Settings) -> Instance:
    try:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": BOOTSTRAP_ADVISORY_LOCK_ID},
        )
        existing = await session.scalar(select(Instance).where(Instance.is_self.is_(True)))
        if existing is not None:
            if existing.domain != settings.domain:
                raise DomainMismatchError(
                    f"configured domain {settings.domain!r} does not match stored identity "
                    f"{existing.domain!r}"
                )
            await verify_stored_identity(session, settings, existing)
            await ensure_message_partitions(await session.connection())
            await session.commit()
            return existing

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        private_bytes = private_key.private_bytes_raw()
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(settings.secret_key_bytes).encrypt(
            nonce, private_bytes, settings.domain.encode("ascii")
        )
        key_id = new_signing_key_id()
        instance = Instance(
            domain=settings.domain,
            is_self=True,
            display_name="Kaede Chat",
            federation_mode=settings.federation_mode,
            current_key_id=key_id,
            encrypted_private_key=encrypted,
            private_key_nonce=nonce,
        )
        session.add(instance)
        session.add(PeerKey(domain=settings.domain, key_id=key_id, public_key=public_key))
        await session.flush()
        await ensure_message_partitions(await session.connection())
        await session.commit()
        return instance
    except Exception:
        await session.rollback()
        raise


async def rotate_instance_signing_key(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> Instance:
    """Atomically install a new current key while retaining verification history."""

    try:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": BOOTSTRAP_ADVISORY_LOCK_ID},
        )
        instance = await session.scalar(select(Instance).where(Instance.is_self.is_(True)))
        if instance is None:
            raise IdentityKeyError("instance bootstrap is required before key rotation")
        if instance.domain != settings.domain:
            raise DomainMismatchError(
                f"configured domain {settings.domain!r} does not match stored identity "
                f"{instance.domain!r}"
            )
        await verify_stored_identity(session, settings, instance)

        rotated_at = signing_key_operation_time(now)
        active_keys = list(
            await session.scalars(
                select(PeerKey)
                .where(
                    PeerKey.domain == settings.domain,
                    PeerKey.expired_at.is_(None),
                )
                .with_for_update()
            )
        )
        current_key = next(
            (key for key in active_keys if key.key_id == instance.current_key_id),
            None,
        )
        if current_key is None:
            raise IdentityKeyError(
                "stored current signing key is missing from verification history"
            )

        overlap = signing_key_retirement_overlap(settings)
        for key in active_keys:
            if key.key_id == instance.current_key_id:
                continue
            if key.retire_after is None:
                # Keys created before retirement deadlines existed start a full
                # conservative overlap window on their first managed rotation.
                key.retire_after = rotated_at + overlap
            elif key.retire_after <= rotated_at:
                pending_deletion_proof = await signing_key_has_pending_deletion_proofs(
                    session,
                    settings,
                    key.key_id,
                )
                if not pending_deletion_proof:
                    key.expired_at = rotated_at
        retained_old_keys = [
            key
            for key in active_keys
            if key.key_id != instance.current_key_id and key.expired_at is None
        ]
        if len(retained_old_keys) >= MAX_ADVERTISED_OLD_KEYS:
            earliest = min(
                (key.retire_after for key in retained_old_keys if key.retire_after is not None),
                default=None,
            )
            suffix = f"; earliest safe retirement is {earliest.isoformat()}" if earliest else ""
            raise IdentityKeyError("signing-key history is at its 64-key safety bound" + suffix)

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(settings.secret_key_bytes).encrypt(
            nonce,
            private_key.private_bytes_raw(),
            settings.domain.encode("ascii"),
        )
        key_id = new_signing_key_id()
        current_key.retire_after = rotated_at + overlap
        instance.current_key_id = key_id
        instance.encrypted_private_key = encrypted
        instance.private_key_nonce = nonce
        session.add(PeerKey(domain=settings.domain, key_id=key_id, public_key=public_key))
        await session.flush()
        await session.commit()
        return instance
    except Exception:
        await session.rollback()
        raise


async def retire_instance_signing_key(
    session: AsyncSession,
    settings: Settings,
    key_id: str,
    *,
    force_compromised: bool = False,
    now: datetime | None = None,
) -> PeerKey:
    """Retire one historical local key, enforcing its signed-data overlap.

    ``force_compromised`` deliberately bypasses the overlap deadline. Operators
    should use it only when the corresponding private key is believed stolen:
    queued envelopes signed by that key can become unverifiable immediately.
    """

    committed = False
    try:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": BOOTSTRAP_ADVISORY_LOCK_ID},
        )
        instance = await session.scalar(select(Instance).where(Instance.is_self.is_(True)))
        if instance is None:
            raise IdentityKeyError("instance bootstrap is required before key retirement")
        if instance.domain != settings.domain:
            raise DomainMismatchError(
                f"configured domain {settings.domain!r} does not match stored identity "
                f"{instance.domain!r}"
            )
        if key_id == instance.current_key_id:
            raise IdentityKeyError("the current signing key must be rotated before it can retire")
        key = await session.scalar(
            select(PeerKey)
            .where(PeerKey.domain == settings.domain, PeerKey.key_id == key_id)
            .with_for_update()
        )
        if key is None:
            raise IdentityKeyError(f"unknown local signing key {key_id!r}")
        if key.expired_at is not None:
            await session.commit()
            committed = True
            return key

        retired_at = signing_key_operation_time(now)
        if key.retire_after is None and not force_compromised:
            key.retire_after = retired_at + signing_key_retirement_overlap(settings)
            await session.commit()
            committed = True
            raise KeyRetirementTooEarly(key.key_id, key.retire_after)
        if not force_compromised and key.retire_after is not None and retired_at < key.retire_after:
            raise KeyRetirementTooEarly(key.key_id, key.retire_after)
        if not force_compromised and await signing_key_has_pending_deletion_proofs(
            session,
            settings,
            key.key_id,
        ):
            raise IdentityKeyError(
                f"signing key {key.key_id!r} still authenticates pending deletion proofs"
            )
        if key.retire_after is None:
            key.retire_after = retired_at
        key.expired_at = retired_at
        await session.commit()
        committed = True
        return key
    except Exception:
        if not committed:
            await session.rollback()
        raise
