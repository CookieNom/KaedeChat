from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from app.api.e2ee import (
    DeviceRegister,
    decode_base64url,
    encode_base64url,
    key_package_signing_input,
    registration_signing_input,
)
from app.chat.e2ee import E2EE_SUITE_MLS_128
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


def test_rolling_upgrade_device_claim_uses_conservative_room_cap() -> None:
    request = E2EEKeyPackageClaimRequest.model_validate(
        {
            "channel_id": "10",
            "channel_domain": "alpha.localhost",
            "claimant_id": "7",
            "claimant_domain": "alpha.localhost",
            "target_id": "17",
            "target_domain": "beta.localhost",
        }
    )
    assert request.max_devices == 48
