from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.bootstrap import (
    BOOTSTRAP_ADVISORY_LOCK_ID,
    IdentityKeyError,
    KeyRetirementTooEarly,
    bootstrap_instance,
    new_signing_key_id,
    retire_instance_signing_key,
    rotate_instance_signing_key,
    signing_key_retirement_overlap,
)
from app.core.settings import Settings
from app.db.models import Instance, PeerKey


def settings(
    secret_key: str = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA",
) -> Settings:
    return Settings(
        domain="alpha.localhost",
        environment="test",
        secret_key=secret_key,
        database_url="postgresql+asyncpg://kaede:secret@postgres/kaede",
        dragonfly_url="redis://dragonfly:6379/0",
    )


def stored_identity(config: Settings) -> tuple[Instance, PeerKey]:
    private_key = Ed25519PrivateKey.generate()
    nonce = b"\x01" * 12
    encrypted = AESGCM(config.secret_key_bytes).encrypt(
        nonce,
        private_key.private_bytes_raw(),
        config.domain.encode("ascii"),
    )
    key_id = "ed25519:20260717"
    return (
        Instance(
            domain=config.domain,
            is_self=True,
            current_key_id=key_id,
            encrypted_private_key=encrypted,
            private_key_nonce=nonce,
        ),
        PeerKey(
            domain=config.domain,
            key_id=key_id,
            public_key=private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        ),
    )


def test_signing_key_ids_are_readable_and_unique_within_one_utc_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_at = datetime(2026, 7, 18, 12, 30, tzinfo=UTC)
    suffixes = iter(("a" * 32, "b" * 32))

    def token_hex(byte_count: int) -> str:
        assert byte_count == 16
        return next(suffixes)

    monkeypatch.setattr("app.bootstrap.secrets.token_hex", token_hex)

    first = new_signing_key_id(generated_at)
    second = new_signing_key_id(generated_at)

    assert re.fullmatch(r"ed25519:20260718-[0-9a-f]{32}", first)
    assert re.fullmatch(r"ed25519:20260718-[0-9a-f]{32}", second)
    assert first != second


@pytest.mark.asyncio
async def test_existing_bootstrap_takes_database_lock_before_identity_lookup() -> None:
    order: list[str] = []
    config = settings()
    instance, peer_key = stored_identity(config)
    session = MagicMock()
    session.execute = AsyncMock(side_effect=lambda *_args: order.append("lock"))
    session.scalar = AsyncMock(side_effect=lambda *_args: (order.append("lookup"), instance)[1])
    session.get = AsyncMock(return_value=peer_key)
    session.connection = AsyncMock(return_value=AsyncMock())
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    instance = await bootstrap_instance(session, config)

    assert instance.domain == "alpha.localhost"
    assert order[:2] == ["lock", "lookup"]
    statement, parameters = session.execute.await_args.args
    assert "pg_advisory_xact_lock" in str(statement)
    assert parameters == {"lock_id": BOOTSTRAP_ADVISORY_LOCK_ID}
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_bootstrap_rejects_wrong_secret_key() -> None:
    config = settings()
    instance, peer_key = stored_identity(config)
    wrong_config = settings("MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE")
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=instance)
    session.get = AsyncMock(return_value=peer_key)
    session.rollback = AsyncMock()

    with pytest.raises(IdentityKeyError, match="KAEDE_SECRET_KEY"):
        await bootstrap_instance(session, wrong_config)

    session.rollback.assert_awaited_once()
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_bootstrap_rejects_inconsistent_public_key() -> None:
    config = settings()
    instance, peer_key = stored_identity(config)
    peer_key.public_key = (
        Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=instance)
    session.get = AsyncMock(return_value=peer_key)
    session.rollback = AsyncMock()

    with pytest.raises(IdentityKeyError, match="inconsistent"):
        await bootstrap_instance(session, config)

    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_key_rotation_retains_old_key_and_installs_matching_new_key() -> None:
    config = settings()
    instance, old_peer_key = stored_identity(config)
    old_key_id = instance.current_key_id
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=instance)
    session.scalars = AsyncMock(return_value=[old_peer_key])
    session.get = AsyncMock(return_value=old_peer_key)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    rotated_at = datetime(2026, 7, 18, 12, 30, tzinfo=UTC)
    rotated = await rotate_instance_signing_key(session, config, now=rotated_at)

    assert rotated.current_key_id != old_key_id
    new_peer_key = session.add.call_args.args[0]
    assert isinstance(new_peer_key, PeerKey)
    assert new_peer_key.key_id == rotated.current_key_id
    decrypted = AESGCM(config.secret_key_bytes).decrypt(
        rotated.private_key_nonce,
        rotated.encrypted_private_key,
        config.domain.encode("ascii"),
    )
    assert (
        Ed25519PrivateKey.from_private_bytes(decrypted)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
        == new_peer_key.public_key
    )
    assert old_peer_key.expired_at is None
    assert old_peer_key.retire_after == rotated_at + signing_key_retirement_overlap(config)
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_key_retirement_persists_a_legacy_deadline_and_refuses_early() -> None:
    config = settings()
    instance, historical_key = stored_identity(config)
    instance.current_key_id = "ed25519:current"
    retired_at = datetime(2026, 7, 18, 12, 30, tzinfo=UTC)
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(side_effect=[instance, historical_key])
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    with pytest.raises(KeyRetirementTooEarly) as raised:
        await retire_instance_signing_key(
            session,
            config,
            historical_key.key_id,
            now=retired_at,
        )

    assert historical_key.retire_after == retired_at + signing_key_retirement_overlap(config)
    assert raised.value.retire_after == historical_key.retire_after
    assert historical_key.expired_at is None
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_key_retirement_requires_deadline_unless_compromise_is_explicit() -> None:
    config = settings()
    retired_at = datetime(2026, 7, 18, 12, 30, tzinfo=UTC)

    instance, historical_key = stored_identity(config)
    instance.current_key_id = "ed25519:current"
    historical_key.retire_after = retired_at + timedelta(days=1)
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(side_effect=[instance, historical_key])
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    with pytest.raises(KeyRetirementTooEarly):
        await retire_instance_signing_key(
            session,
            config,
            historical_key.key_id,
            now=retired_at,
        )
    assert historical_key.expired_at is None
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()

    instance, compromised_key = stored_identity(config)
    instance.current_key_id = "ed25519:current"
    compromised_key.retire_after = retired_at + timedelta(days=1)
    forced_session = MagicMock()
    forced_session.execute = AsyncMock()
    forced_session.scalar = AsyncMock(side_effect=[instance, compromised_key])
    forced_session.commit = AsyncMock()
    forced_session.rollback = AsyncMock()

    await retire_instance_signing_key(
        forced_session,
        config,
        compromised_key.key_id,
        force_compromised=True,
        now=retired_at,
    )

    assert compromised_key.expired_at == retired_at
    forced_session.commit.assert_awaited_once()
    forced_session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_key_retirement_refuses_pending_durable_deletion_proofs() -> None:
    config = settings()
    retired_at = datetime(2026, 8, 18, 12, 30, tzinfo=UTC)
    instance, historical_key = stored_identity(config)
    instance.current_key_id = "ed25519:current"
    historical_key.retire_after = retired_at - timedelta(seconds=1)
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(side_effect=[instance, historical_key, True])
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    with pytest.raises(IdentityKeyError, match="pending deletion proofs"):
        await retire_instance_signing_key(
            session,
            config,
            historical_key.key_id,
            now=retired_at,
        )

    assert historical_key.expired_at is None
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()
