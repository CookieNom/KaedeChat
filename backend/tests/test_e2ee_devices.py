from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import e2ee as e2ee_api
from app.api import federation as federation_api
from app.api.e2ee import (
    AccountEncryptionReset,
    AccountVaultEnvelope,
    AccountVaultWrite,
    DeviceRegister,
    acquire_account_vault_lease,
    clear_recovery_authorization,
    decode_base64url,
    encode_base64url,
    issue_recovery_authorization,
    key_package_signing_input,
    register_device,
    registration_signing_input,
    require_portable_identity_slot,
    require_recovery_authorization,
    require_recovery_enrollment_session,
    require_recovery_reset_session,
    reset_account_encryption,
    revoke_device,
    update_account_vault,
)
from app.chat.e2ee import E2EE_SUITE_MLS_128
from app.core.permissions import Permission
from app.db.models import User
from app.federation.schemas import E2EEKeyPackageClaimRequest


def test_device_registration_proof_binds_account_session_key_and_credential() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw()
    user = cast(User, SimpleNamespace(id=17, origin_domain="alpha.localhost"))
    credential_digest = hashlib.sha256(b"credential").digest()
    signing_input = registration_signing_input(
        b"c" * 32,
        user,
        "session-one",
        public_key,
        credential_digest,
    )
    signature = private_key.sign(signing_input)
    private_key.public_key().verify(signature, signing_input)

    changed = registration_signing_input(
        b"c" * 32,
        user,
        "session-one",
        public_key,
        hashlib.sha256(b"different").digest(),
    )
    with pytest.raises(InvalidSignature):
        private_key.public_key().verify(signature, changed)


def test_key_package_upload_proof_binds_order_expiry_and_device() -> None:
    private_key = Ed25519PrivateKey.generate()
    digests = [hashlib.sha256(b"one").digest(), hashlib.sha256(b"two").digest()]
    expires = datetime(2026, 8, 20, tzinfo=UTC)
    signing_input = key_package_signing_input(
        "ked_device",
        E2EE_SUITE_MLS_128,
        expires,
        digests,
    )
    signature = private_key.sign(signing_input)
    private_key.public_key().verify(signature, signing_input)
    with pytest.raises(InvalidSignature):
        private_key.public_key().verify(
            signature,
            key_package_signing_input(
                "ked_device",
                E2EE_SUITE_MLS_128,
                expires,
                list(reversed(digests)),
            ),
        )


def test_key_package_upload_uses_cross_client_millisecond_timestamp() -> None:
    expires = datetime(2026, 8, 20, 12, 34, 56, 123000, tzinfo=UTC)
    signing_input = key_package_signing_input(
        "ked_device",
        E2EE_SUITE_MLS_128,
        expires,
        [hashlib.sha256(b"package").digest()],
    )
    assert b"2026-08-20T12:34:56.123+00:00" in signing_input
    assert b".123000+00:00" not in signing_input


def test_base64url_decoder_rejects_noncanonical_input() -> None:
    encoded = encode_base64url(b"a" * 32)
    assert decode_base64url(encoded, size=32) == b"a" * 32
    with pytest.raises(ValueError, match="canonical"):
        decode_base64url(encoded + "=", size=32)


def test_device_registration_requires_mls_capability() -> None:
    private_key = Ed25519PrivateKey.generate()
    identity_key = encode_base64url(private_key.public_key().public_bytes_raw())
    with pytest.raises(ValidationError, match="e2ee-mls/1"):
        DeviceRegister(
            challenge_id="x" * 32,
            identity_key=identity_key,
            credential=encode_base64url(b"credential"),
            signature=encode_base64url(b"s" * 64),
            device_name="Browser",
            platform="web",
            capabilities=["e2ee-media/1"],
        )


def test_device_registration_requires_canonical_recovery_authorization() -> None:
    private_key = Ed25519PrivateKey.generate()
    identity_key = encode_base64url(private_key.public_key().public_bytes_raw())
    valid = DeviceRegister(
        challenge_id="x" * 32,
        identity_key=identity_key,
        credential=encode_base64url(b"credential"),
        signature=encode_base64url(b"s" * 64),
        device_name="Browser",
        platform="web",
        capabilities=["e2ee-mls/1"],
        recovery_authorization="ker_" + encode_base64url(b"r" * 32),
    )
    assert valid.recovery_authorization == "ker_" + encode_base64url(b"r" * 32)
    with pytest.raises(ValidationError, match="recovery authorization"):
        DeviceRegister(
            challenge_id="x" * 32,
            identity_key=identity_key,
            credential=encode_base64url(b"credential"),
            signature=encode_base64url(b"s" * 64),
            device_name="Browser",
            platform="web",
            capabilities=["e2ee-mls/1"],
            recovery_authorization="bad_" + "r" * 43,
        )


def test_account_allows_only_one_portable_active_mls_identity() -> None:
    require_portable_identity_slot(0)
    with pytest.raises(HTTPException) as caught:
        require_portable_identity_slot(1)
    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_DEVICE_LIMIT_REACHED"}


def test_recovery_authorization_is_session_generation_and_expiry_bound() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        e2ee_device_generation=4,
    )
    authorization = issue_recovery_authorization(user, "session-one", now)

    assert authorization.startswith("ker_")
    assert authorization.encode() not in (user.e2ee_recovery_token_hash or b"")
    assert require_recovery_enrollment_session(user, "session-one", now) == (
        user.e2ee_recovery_token_hash
    )
    require_recovery_authorization(user, "session-one", authorization, now)
    with pytest.raises(HTTPException) as fenced:
        require_recovery_enrollment_session(user, "session-two", now)
    assert fenced.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}

    for session_id, candidate, checked_at, generation in (
        ("session-two", authorization, now, 4),
        ("session-one", None, now, 4),
        ("session-one", "ker_" + encode_base64url(b"w" * 32), now, 4),
        ("session-one", authorization, now + timedelta(seconds=301), 4),
        ("session-one", authorization, now, 5),
    ):
        user.e2ee_device_generation = generation
        with pytest.raises(HTTPException) as caught:
            require_recovery_authorization(user, session_id, candidate, checked_at)
        assert caught.value.status_code == 409
        assert caught.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}

    user.e2ee_device_generation = 4
    clear_recovery_authorization(user)
    assert require_recovery_enrollment_session(user, "session-two", now) is None
    with pytest.raises(HTTPException):
        require_recovery_authorization(user, "session-one", authorization, now)


def test_unexpired_recovery_fence_rejects_competing_reset_session() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        e2ee_device_generation=4,
    )
    issue_recovery_authorization(user, "reset-session", now)

    require_recovery_reset_session(user, "reset-session", now)
    with pytest.raises(HTTPException) as caught:
        require_recovery_reset_session(user, "stale-session", now)
    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}

    # A generation change cannot let another still-signed-in session steal an
    # unexpired reset. The initiating session can explicitly restart it, and
    # another session may supersede it only after its short lifetime.
    user.e2ee_device_generation += 1
    with pytest.raises(HTTPException) as generation_changed:
        require_recovery_reset_session(user, "stale-session", now)
    assert generation_changed.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}
    require_recovery_reset_session(user, "reset-session", now)
    require_recovery_reset_session(user, "stale-session", now + timedelta(seconds=301))


@pytest.mark.asyncio
async def test_stale_session_cannot_revoke_device_during_pending_recovery() -> None:
    now = datetime.now(UTC)
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        e2ee_device_generation=5,
    )
    issue_recovery_authorization(user, "reset-session", now)
    e2ee_api.consume_recovery_authorization(user)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=user),
        commit=AsyncMock(),
    )

    with pytest.raises(HTTPException) as caught:
        await revoke_device(
            "ked_" + encode_base64url(b"d" * 32),
            auth=SimpleNamespace(
                user=user,
                grant=SimpleNamespace(session_id="stale-session"),
            ),
            session=session,
            redis=SimpleNamespace(),
            settings=SimpleNamespace(),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}
    session.scalar.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("vault_revision", [None, 1])
async def test_stale_session_cannot_win_reactivation_and_fence_waits_for_both_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    vault_revision: int | None,
) -> None:
    now = datetime.now(UTC)
    private_key = Ed25519PrivateKey.generate()
    identity_key = private_key.public_key().public_bytes_raw()
    credential = b"portable-account-credential"
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        e2ee_device_generation=4,
    )
    authorization = issue_recovery_authorization(user, "reset-session", now)
    device = SimpleNamespace(
        id="ked_" + encode_base64url(hashlib.sha256(b"device").digest()),
        user_id=user.id,
        user_domain=user.origin_domain,
        identity_key=identity_key,
        credential=credential,
        device_name="Old browser",
        platform="web",
        capabilities=["e2ee-mls/1", "e2ee-media/1"],
        trust_state="unverified",
        registered_session_id="stale-session",
        device_generation=3,
        last_seen_at=now - timedelta(days=1),
        created_at=now - timedelta(days=2),
        revoked_at=now - timedelta(minutes=1),
    )
    monkeypatch.setattr(
        e2ee_api,
        "pause_local_e2ee_for_device_change",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        e2ee_api,
        "queue_device_change_updates",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(e2ee_api, "publish_e2ee_policy_updates", AsyncMock())
    monkeypatch.setattr(e2ee_api, "publish_dispatch", AsyncMock())
    monkeypatch.setattr(e2ee_api.secrets, "token_bytes", lambda size: b"t" * size)

    async def attempt(
        session_id: str,
        recovery_authorization: str | None,
    ) -> tuple[dict[str, object], SimpleNamespace]:
        signing_input = registration_signing_input(
            b"c" * 32,
            user,
            session_id,
            identity_key,
            hashlib.sha256(credential).digest(),
        )
        challenge = {
            "user_id": str(user.id),
            "user_domain": user.origin_domain,
            "session_id": session_id,
            "identity_key": encode_base64url(identity_key),
            "credential_digest": encode_base64url(hashlib.sha256(credential).digest()),
            "signing_input": encode_base64url(signing_input),
        }
        commit_state: list[tuple[object, object, object, object]] = []

        async def commit() -> None:
            commit_state.append(
                (
                    user.e2ee_recovery_token_hash,
                    user.e2ee_recovery_session_id,
                    user.e2ee_recovery_generation,
                    user.e2ee_recovery_expires_at,
                )
            )

        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[user, device, vault_revision]),
            scalars=AsyncMock(return_value=[]),
            commit=AsyncMock(side_effect=commit),
        )
        redis = SimpleNamespace(getdel=AsyncMock(return_value=json.dumps(challenge)))
        payload = DeviceRegister(
            challenge_id=secrets.token_urlsafe(24),
            identity_key=encode_base64url(identity_key),
            credential=encode_base64url(credential),
            signature=encode_base64url(private_key.sign(signing_input)),
            device_name="Recovered identity",
            platform="web",
            capabilities=["e2ee-mls/1", "e2ee-media/1"],
            recovery_authorization=recovery_authorization,
        )
        rendered = await register_device(
            payload,
            auth=SimpleNamespace(
                user=user,
                grant=SimpleNamespace(session_id=session_id),
            ),
            session=db,
            redis=redis,
            settings=SimpleNamespace(domain=user.origin_domain),
        )
        return rendered, SimpleNamespace(db=db, commit_state=commit_state)

    with pytest.raises(HTTPException) as stale:
        await attempt("stale-session", None)
    assert stale.value.status_code == 409
    assert stale.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}
    assert device.revoked_at is not None
    assert user.e2ee_recovery_token_hash is not None

    rendered, committed = await attempt("reset-session", authorization)
    assert rendered["id"] == device.id
    assert rendered["revoked_at"] is None
    assert committed.db.commit.await_count == 1
    assert user.e2ee_device_generation == 5
    if vault_revision is None:
        # Device-first recovery consumes the bearer but keeps the durable
        # session fence until the revision-one vault is committed.
        assert committed.commit_state == [
            (
                b"t" * 32,
                "reset-session",
                5,
                user.e2ee_recovery_expires_at,
            )
        ]
    else:
        # Vault-first recovery now has both artifacts and can clear the fence.
        assert committed.commit_state == [(None, None, None, None)]

    device.revoked_at = datetime.now(UTC)
    with pytest.raises(HTTPException) as replay:
        require_recovery_authorization(
            user,
            "reset-session",
            authorization,
            datetime.now(UTC),
        )
    assert replay.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}


def test_operation_bound_device_claim_uses_conservative_room_cap() -> None:
    request = E2EEKeyPackageClaimRequest.model_validate(
        {
            "operation_id": "keo_" + "o" * 43,
            "operation_domain": "alpha.localhost",
            "channel_id": "10",
            "channel_domain": "alpha.localhost",
            "claimant_id": "7",
            "claimant_domain": "alpha.localhost",
            "target_id": "17",
            "target_domain": "beta.localhost",
        }
    )
    assert request.max_devices == 48


@pytest.mark.asyncio
async def test_guild_key_package_claim_checks_exact_channel_visibility_for_both_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace()
    redis = SimpleNamespace()
    guild = SimpleNamespace(id=10, origin_domain="alpha.localhost")
    channel = SimpleNamespace(id=20, origin_domain="alpha.localhost")
    claimant = SimpleNamespace(id=30, origin_domain="gamma.localhost")
    target = SimpleNamespace(id=40, origin_domain="beta.localhost")
    permission_check = AsyncMock()
    monkeypatch.setattr(federation_api, "require_permissions", permission_check)

    await federation_api.require_guild_key_package_claim_visibility(
        session,
        redis,
        guild,
        channel,
        claimant,
        target,
    )

    permission_check.assert_has_awaits(
        [
            call(
                session,
                redis,
                guild,
                claimant,
                Permission.VIEW_CHANNEL,
                channel=channel,
            ),
            call(
                session,
                redis,
                guild,
                target,
                Permission.VIEW_CHANNEL,
                channel=channel,
            ),
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("denied_user", ["claimant", "target"])
async def test_guild_key_package_claim_visibility_denial_is_fail_closed_not_found(
    monkeypatch: pytest.MonkeyPatch,
    denied_user: str,
) -> None:
    permission_denied = HTTPException(
        status_code=403,
        detail={"code": "MISSING_PERMISSIONS"},
    )
    permission_check = AsyncMock(
        side_effect=(
            [permission_denied] if denied_user == "claimant" else [None, permission_denied]
        )
    )
    monkeypatch.setattr(federation_api, "require_permissions", permission_check)

    with pytest.raises(HTTPException) as caught:
        await federation_api.require_guild_key_package_claim_visibility(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(id=10, origin_domain="alpha.localhost"),
            SimpleNamespace(id=20, origin_domain="alpha.localhost"),
            SimpleNamespace(id=30, origin_domain="gamma.localhost"),
            SimpleNamespace(id=40, origin_domain="beta.localhost"),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == {"code": "KAED_E2EE_TARGET_NOT_FOUND"}
    assert permission_check.await_count == (1 if denied_user == "claimant" else 2)


def test_account_vault_requires_canonical_authenticated_ciphertext_and_cas_revision() -> None:
    envelope = AccountVaultEnvelope(
        version=2,
        cipher="AES-256-GCM",
        sequence="1",
        nonce=encode_base64url(b"n" * 12),
        ciphertext=encode_base64url(b"ciphertext-and-tag"),
    )
    write = AccountVaultWrite(
        lease_token=encode_base64url(b"l" * 32),
        expected_revision="0",
        envelope=envelope,
    )
    assert write.expected_revision == "0"
    with pytest.raises(ValidationError):
        AccountVaultEnvelope(
            version=2,
            cipher="AES-256-GCM",
            sequence="1",
            nonce=encode_base64url(b"short"),
            ciphertext=envelope.ciphertext,
        )
    with pytest.raises(ValidationError):
        AccountVaultWrite(
            lease_token=write.lease_token,
            expected_revision="01",
            envelope=envelope,
        )
    with pytest.raises(ValidationError, match="sequence"):
        AccountVaultWrite(
            lease_token=write.lease_token,
            expected_revision="1",
            envelope=envelope,
        )


@pytest.mark.asyncio
async def test_stale_session_cannot_acquire_vault_lease_during_recovery() -> None:
    now = datetime.now(UTC)
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        e2ee_device_generation=4,
    )
    issue_recovery_authorization(user, "reset-session", now)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=user),
        get=AsyncMock(),
    )
    redis = SimpleNamespace(set=AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await acquire_account_vault_lease(
            auth=SimpleNamespace(
                user=user,
                grant=SimpleNamespace(session_id="stale-session"),
            ),
            session=session,
            redis=redis,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}
    session.scalar.assert_awaited_once()
    redis.set.assert_not_awaited()
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_session_cannot_write_vault_during_recovery() -> None:
    now = datetime.now(UTC)
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        e2ee_device_generation=4,
    )
    issue_recovery_authorization(user, "reset-session", now)
    payload = AccountVaultWrite(
        lease_token=encode_base64url(b"s" * 32),
        expected_revision="0",
        envelope=AccountVaultEnvelope(
            version=2,
            cipher="AES-256-GCM",
            sequence="1",
            nonce=encode_base64url(b"n" * 12),
            ciphertext=encode_base64url(b"ciphertext-and-tag"),
        ),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=user),
        add=MagicMock(),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace(get=AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await update_account_vault(
            payload,
            auth=SimpleNamespace(
                user=user,
                grant=SimpleNamespace(session_id="stale-session"),
            ),
            session=session,
            redis=redis,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}
    redis.get.assert_not_awaited()
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("device_first", [False, True])
async def test_revision_one_vault_write_clears_fence_only_after_device_artifact(
    monkeypatch: pytest.MonkeyPatch,
    device_first: bool,
) -> None:
    now = datetime.now(UTC)
    lease_token = encode_base64url(b"l" * 32)
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        e2ee_device_generation=4,
    )
    original_authorization = issue_recovery_authorization(user, "reset-session", now)
    original_digest = user.e2ee_recovery_token_hash
    active_devices = (
        [
            SimpleNamespace(
                id="ked_" + encode_base64url(b"d" * 32),
                device_generation=4,
                registered_session_id="reset-session",
                revoked_at=None,
            )
        ]
        if device_first
        else []
    )
    payload = AccountVaultWrite(
        lease_token=lease_token,
        expected_revision="0",
        envelope=AccountVaultEnvelope(
            version=2,
            cipher="AES-256-GCM",
            sequence="1",
            nonce=encode_base64url(b"n" * 12),
            ciphertext=encode_base64url(b"ciphertext-and-tag"),
        ),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[user, None]),
        scalars=AsyncMock(return_value=active_devices),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace(get=AsyncMock(return_value=lease_token))
    monkeypatch.setattr(e2ee_api, "render_account_vault", lambda _: {"revision": "1"})

    result = await update_account_vault(
        payload,
        auth=SimpleNamespace(
            user=user,
            grant=SimpleNamespace(session_id="reset-session"),
        ),
        session=session,
        redis=redis,
    )

    assert result == {"vault": {"revision": "1"}}
    session.commit.assert_awaited_once()
    # The cleartext bearer is never stored; retaining the original digest in
    # the vault-first crash state allows the same session to finish device
    # enrollment, while every other session remains fenced out.
    assert original_authorization.encode() not in (original_digest or b"")
    if device_first:
        assert user.e2ee_recovery_token_hash is None
        assert user.e2ee_recovery_session_id is None
        assert user.e2ee_recovery_generation is None
        assert user.e2ee_recovery_expires_at is None
    else:
        assert user.e2ee_recovery_token_hash == original_digest
        assert user.e2ee_recovery_session_id == "reset-session"
        assert user.e2ee_recovery_generation == 4
        assert user.e2ee_recovery_expires_at is not None


@pytest.mark.asyncio
async def test_device_first_crash_keeps_stale_session_fenced_from_vault() -> None:
    now = datetime.now(UTC)
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        e2ee_device_generation=5,
    )
    issue_recovery_authorization(user, "reset-session", now)
    # Model the committed device half: the bearer has been consumed and the
    # durable fence now follows the registered device generation.
    e2ee_api.consume_recovery_authorization(user)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=user),
        get=AsyncMock(),
    )
    redis = SimpleNamespace(set=AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await acquire_account_vault_lease(
            auth=SimpleNamespace(
                user=user,
                grant=SimpleNamespace(session_id="stale-session"),
            ),
            session=session,
            redis=redis,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_expected_revision_zero_write_cannot_repopulate_after_reset_fence() -> None:
    stale_token = encode_base64url(b"s" * 32)
    reset_token = encode_base64url(b"r" * 32)
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
    )
    payload = AccountVaultWrite(
        lease_token=stale_token,
        expected_revision="0",
        envelope=AccountVaultEnvelope(
            version=2,
            cipher="AES-256-GCM",
            sequence="1",
            nonce=encode_base64url(b"n" * 12),
            ciphertext=encode_base64url(b"ciphertext-and-tag"),
        ),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=user),
        add=MagicMock(),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace(get=AsyncMock(return_value=reset_token))

    with pytest.raises(HTTPException) as caught:
        await update_account_vault(
            payload,
            auth=SimpleNamespace(
                user=user,
                grant=SimpleNamespace(session_id="session-one"),
            ),
            session=session,
            redis=redis,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_ACCOUNT_VAULT_LEASE_EXPIRED"}
    session.scalar.assert_awaited_once()
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_authenticated_encryption_reset_clears_vault_ancestry_and_returns_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    random_token = encode_base64url(b"r" * 32)
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        e2ee_device_generation=3,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=user),
        scalars=AsyncMock(return_value=[]),
        execute=AsyncMock(),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace(
        set=AsyncMock(return_value=True),
        get=AsyncMock(return_value=random_token),
        eval=AsyncMock(return_value=1),
    )
    monkeypatch.setattr(e2ee_api.secrets, "token_urlsafe", lambda _: random_token)
    monkeypatch.setattr(
        e2ee_api,
        "pause_local_e2ee_for_device_change",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        e2ee_api,
        "queue_device_change_updates",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(e2ee_api, "publish_e2ee_policy_updates", AsyncMock())
    monkeypatch.setattr(e2ee_api, "publish_dispatch", AsyncMock())

    result = await reset_account_encryption(
        AccountEncryptionReset(confirmation="RESET ENCRYPTED HISTORY"),
        auth=SimpleNamespace(user=user, grant=SimpleNamespace(session_id="session-one")),
        session=session,
        redis=redis,
        settings=SimpleNamespace(domain="alpha.localhost"),
    )

    assert result == {
        "status": "encryption_reset",
        "account_ref": "17@alpha.localhost",
        "recovery_authorization": "ker_" + random_token,
        "recovery_authorization_expires_in": 300,
    }
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert any("DELETE FROM e2ee_account_vault_digests" in item for item in statements)
    assert any("DELETE FROM e2ee_account_vaults" in item for item in statements)
    assert user.e2ee_device_generation == 4
    assert user.e2ee_recovery_session_id == "session-one"
    assert user.e2ee_recovery_generation == 4
    assert user.e2ee_recovery_token_hash == e2ee_api.recovery_authorization_digest(
        "ker_" + random_token
    )
    assert user.e2ee_recovery_expires_at is not None


@pytest.mark.asyncio
async def test_competing_session_cannot_replace_unexpired_reset_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
        e2ee_device_generation=3,
    )
    issue_recovery_authorization(user, "reset-session", datetime.now(UTC))
    lease_token = encode_base64url(b"l" * 32)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=user),
        scalars=AsyncMock(),
        execute=AsyncMock(),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace(
        set=AsyncMock(return_value=True),
        get=AsyncMock(return_value=lease_token),
        eval=AsyncMock(return_value=1),
    )
    monkeypatch.setattr(e2ee_api.secrets, "token_urlsafe", lambda _: lease_token)

    with pytest.raises(HTTPException) as caught:
        await reset_account_encryption(
            AccountEncryptionReset(confirmation="RESET ENCRYPTED HISTORY"),
            auth=SimpleNamespace(
                user=user,
                grant=SimpleNamespace(session_id="stale-session"),
            ),
            session=session,
            redis=redis,
            settings=SimpleNamespace(domain="alpha.localhost"),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_RECOVERY_AUTHORIZATION_REQUIRED"}
    session.scalars.assert_not_awaited()
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
    redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_encryption_reset_rechecks_its_sentinel_after_durable_user_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=17,
        origin_domain="alpha.localhost",
        is_local=True,
        username="maple",
        account_type="human",
        password_hash="hash",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=user),
        execute=AsyncMock(),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace(
        set=AsyncMock(return_value=True),
        get=AsyncMock(return_value="new-holder"),
        eval=AsyncMock(return_value=0),
    )
    monkeypatch.setattr(e2ee_api.secrets, "token_urlsafe", lambda _: "stale-holder")

    with pytest.raises(HTTPException) as caught:
        await reset_account_encryption(
            AccountEncryptionReset(confirmation="RESET ENCRYPTED HISTORY"),
            auth=SimpleNamespace(user=user),
            session=session,
            redis=redis,
            settings=SimpleNamespace(domain="alpha.localhost"),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_ACCOUNT_VAULT_LEASE_EXPIRED"}
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
    redis.eval.assert_awaited_once()
