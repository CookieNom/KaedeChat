from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import e2ee as e2ee_api
from app.api.e2ee import (
    RoomActivationRequest,
    RoomProposalRequest,
    account_vault_chain_root,
    account_vault_digest,
    apply_e2ee_control_metadata,
    encode_base64url,
    get_account_vault_digests,
    protocol_request_digest,
    require_prepared_account_vault,
    validate_remote_room_commit_response,
)
from app.db.models import (
    Channel,
    E2EEAccountVault,
    E2EEAccountVaultDigest,
    E2EEControlRecord,
    Message,
)
from app.federation.schemas import E2EERoomOperationStatusRequest, E2EERoomProxyRequest

OPERATION_ID = "keo_" + "o" * 43
DEVICE_ID = "ked_" + "d" * 43
VAULT_DIGEST = encode_base64url(b"v" * 32)
GROUP_ID = encode_base64url(b"g" * 32)


def _actor() -> dict[str, object]:
    return {
        "id": "7",
        "origin_domain": "beta.localhost",
        "username": "remote_user",
        "profile_version": 1,
        "e2ee_device_generation": 1,
    }


def test_room_operation_requests_require_canonical_operation_and_vault_context() -> None:
    proposal = RoomProposalRequest(
        operation_id=OPERATION_ID,
        sender_device_id=DEVICE_ID,
    )
    activation = RoomActivationRequest(
        **proposal.model_dump(),
        policy_generation="2",
        epoch="1",
        group_id=GROUP_ID,
        commit=encode_base64url(b"commit"),
        welcome=encode_base64url(b"welcome"),
        prepared_vault_revision="3",
        prepared_vault_digest=VAULT_DIGEST,
        vault_lease_token=encode_base64url(b"l" * 32),
    )
    assert activation.operation_id == OPERATION_ID
    assert activation.prepared_vault_revision == "3"
    with pytest.raises(ValidationError):
        RoomProposalRequest(
            operation_id="keo_short",
            sender_device_id=DEVICE_ID,
        )
    with pytest.raises(ValidationError):
        RoomActivationRequest.model_validate(
            {**activation.model_dump(), "prepared_vault_digest": VAULT_DIGEST + "="}
        )


def test_control_capture_migration_defaults_unreconciled_controls_to_audit() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations/versions/e5c7b9a1d204_fail_closed_e2ee_control_capture.py"
    ).read_text()
    assert 'down_revision: str | None = "a1c6e8f2d940"' in migration
    assert (
        '_create_control_capture_function(welcome_mode="audit", commit_mode="audit")' in migration
    )
    assert "WHERE room_operation_id IS NULL" in migration


def test_federation_activation_requires_complete_positive_attestation() -> None:
    common = {
        "channel_id": "10",
        "channel_domain": "alpha.localhost",
        "actor": _actor(),
        "operation_id": OPERATION_ID,
        "sender_device_id": DEVICE_ID,
    }
    proposal = E2EERoomProxyRequest.model_validate(common)
    assert proposal.vault_attested is None
    activation = E2EERoomProxyRequest.model_validate(
        {
            **common,
            "policy_generation": "2",
            "epoch": "1",
            "group_id": GROUP_ID,
            "commit": encode_base64url(b"commit"),
            "welcome": encode_base64url(b"welcome"),
            "prepared_vault_revision": "3",
            "prepared_vault_digest": VAULT_DIGEST,
            "vault_attested": True,
        }
    )
    assert activation.vault_attested is True
    with pytest.raises(ValidationError, match="incomplete"):
        E2EERoomProxyRequest.model_validate({**common, "policy_generation": "2"})
    with pytest.raises(ValidationError, match="attestation"):
        E2EERoomProxyRequest.model_validate(
            {
                **activation.model_dump(),
                "vault_attested": False,
            }
        )


def _remote_commit_result() -> tuple[dict[str, object], dict[str, object]]:
    rendered: dict[str, object] = {
        "id": "10",
        "origin_domain": "alpha.localhost",
        "operation_id": OPERATION_ID,
        "operation_status": "committed",
        "encryption_mode": "e2ee",
        "encryption_state": "active",
        "encryption_policy_generation": "2",
        "encryption_protocol": "mls10",
        "encryption_suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
        "encryption_group_id": GROUP_ID,
        "encryption_epoch": "1",
        "controls": [
            {
                "id": "101",
                "origin_domain": "alpha.localhost",
                "operation": "welcome",
                "apply": True,
            },
            {
                "id": "102",
                "origin_domain": "alpha.localhost",
                "operation": "commit",
                "apply": False,
            },
        ],
    }
    prepared: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "status": "prepared",
        "policy": {
            "mode": "plaintext",
            "state": "proposed",
            "generation": "2",
            "protocol": "mls10",
            "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
            "group_id": GROUP_ID,
            "epoch": None,
        },
        "key_packages": [],
    }
    status: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "kind": "activate",
        "status": "committed",
        "prepared": prepared,
        "committed": rendered,
    }
    return rendered, status


def test_remote_commit_response_is_bound_before_projection() -> None:
    rendered, status = _remote_commit_result()
    channel = cast(
        Channel,
        SimpleNamespace(id=10, origin_domain="alpha.localhost"),
    )
    validate_remote_room_commit_response(
        rendered,
        status,
        kind="activate",
        operation_id=OPERATION_ID,
        channel=channel,
        policy_generation="2",
        group_id=GROUP_ID,
        authority="alpha.localhost",
    )


@pytest.mark.parametrize(
    ("target", "key", "value"),
    [
        ("rendered", "operation_id", "keo_" + "x" * 43),
        ("rendered", "encryption_group_id", encode_base64url(b"x" * 32)),
        ("status", "status", "prepared"),
        ("commit", "apply", True),
        ("welcome", "origin_domain", "beta.localhost"),
    ],
)
def test_remote_commit_response_rejects_unbound_authority_results(
    target: str,
    key: str,
    value: object,
) -> None:
    rendered, status = deepcopy(_remote_commit_result())
    if target == "rendered":
        rendered[key] = value
    elif target == "status":
        status[key] = value
    else:
        controls = cast(list[dict[str, object]], rendered["controls"])
        controls[0 if target == "welcome" else 1][key] = value
    channel = cast(
        Channel,
        SimpleNamespace(id=10, origin_domain="alpha.localhost"),
    )
    with pytest.raises(HTTPException) as caught:
        validate_remote_room_commit_response(
            rendered,
            status,
            kind="activate",
            operation_id=OPERATION_ID,
            channel=channel,
            policy_generation="2",
            group_id=GROUP_ID,
            authority="alpha.localhost",
        )
    assert caught.value.status_code == 502
    assert caught.value.detail == {"code": "E2EE_ROOM_AUTHORITY_INVALID_RESPONSE"}


def test_federation_operation_status_is_actor_and_channel_bound() -> None:
    request = E2EERoomOperationStatusRequest.model_validate(
        {
            "channel_id": "10",
            "channel_domain": "alpha.localhost",
            "actor": _actor(),
            "operation_id": OPERATION_ID,
        }
    )
    assert request.operation_id == OPERATION_ID
    assert request.actor.origin_domain == "beta.localhost"


def test_protocol_request_digest_is_canonical_and_context_bound() -> None:
    first = protocol_request_digest("label", {"b": 2, "a": "one"})
    reordered = protocol_request_digest("label", {"a": "one", "b": 2})
    changed_label = protocol_request_digest("other", {"a": "one", "b": 2})
    changed_value = protocol_request_digest("label", {"a": "one", "b": 3})
    assert first == reordered
    assert first != changed_label
    assert first != changed_value


def test_account_vault_digest_binds_format_nonce_and_ciphertext() -> None:
    vault = cast(
        E2EEAccountVault,
        SimpleNamespace(
            format_version=2,
            revision=7,
            nonce=b"n" * 12,
            ciphertext=b"ciphertext",
        ),
    )
    digest = account_vault_digest(vault)
    assert len(digest) == 32
    assert digest.hex() == "6448c7e03807d27468a4276d5bca771bcf8d8ed1620922cec03dd3546fbd782f"
    changed = cast(
        E2EEAccountVault,
        SimpleNamespace(
            format_version=2,
            revision=7,
            nonce=b"m" * 12,
            ciphertext=b"ciphertext",
        ),
    )
    assert digest != account_vault_digest(changed)
    changed_revision = cast(
        E2EEAccountVault,
        SimpleNamespace(
            format_version=2,
            revision=8,
            nonce=b"n" * 12,
            ciphertext=b"ciphertext",
        ),
    )
    assert digest != account_vault_digest(changed_revision)


def test_account_vault_chain_root_matches_mobile_and_web_vector() -> None:
    digest = bytes.fromhex("02a2c5fecb100b0c89e61b1b6bac265503e8ab3933958d1ebfd561e02af67b96")
    assert encode_base64url(account_vault_chain_root(bytes(32), 1, digest)) == (
        "CAEkikOBbzZQ0cRXCHB9tNKIKtLoERyk6okiTTReHcU"
    )
    with pytest.raises(ValueError):
        account_vault_chain_root(bytes(31), 1, digest)


@pytest.mark.asyncio
async def test_account_vault_digest_page_is_strict_consecutive_and_canonical() -> None:
    rows = [
        E2EEAccountVaultDigest(
            user_id=7,
            user_domain="alpha.localhost",
            user_is_local=True,
            revision=revision,
            digest=bytes([revision]) * 32,
        )
        for revision in (3, 4, 5)
    ]
    session = SimpleNamespace(scalars=AsyncMock(return_value=rows))
    auth = SimpleNamespace(user=SimpleNamespace(id=7, origin_domain="alpha.localhost"))

    page = await get_account_vault_digests(
        after=2,
        limit=2,
        auth=auth,
        session=session,
    )

    assert page == {
        "digests": [
            {"revision": "3", "digest": encode_base64url(bytes([3]) * 32)},
            {"revision": "4", "digest": encode_base64url(bytes([4]) * 32)},
        ],
        "next_after": "4",
    }

    session.scalars = AsyncMock(return_value=[rows[0], rows[2]])
    with pytest.raises(RuntimeError, match="not consecutive"):
        await get_account_vault_digests(
            after=2,
            limit=2,
            auth=auth,
            session=session,
        )


@pytest.mark.asyncio
async def test_prepared_vault_rechecks_redis_lease_after_durable_user_lock() -> None:
    events: list[str] = []
    user = SimpleNamespace(id=7, origin_domain="alpha.localhost", is_local=True)
    vault = cast(
        E2EEAccountVault,
        SimpleNamespace(
            format_version=2,
            revision=1,
            nonce=b"n" * 12,
            ciphertext=b"opaque-vault",
        ),
    )
    token = encode_base64url(b"l" * 32)

    async def scalar(_: object) -> object:
        events.append("user-lock" if not events else "vault-lock")
        return user if len(events) == 1 else vault

    async def get(_: str) -> str:
        events.append("redis-read")
        return token

    session = SimpleNamespace(scalar=scalar)
    redis = SimpleNamespace(get=get)

    await require_prepared_account_vault(
        session,
        redis,
        user,
        lease_token=token,
        revision="1",
        digest=encode_base64url(account_vault_digest(vault)),
    )

    assert events == ["user-lock", "redis-read", "vault-lock"]


@pytest.mark.asyncio
async def test_stale_vault_holder_is_fenced_if_lease_changes_while_waiting_for_lock() -> None:
    events: list[str] = []
    user = SimpleNamespace(id=7, origin_domain="alpha.localhost", is_local=True)
    stale_token = encode_base64url(b"s" * 32)
    current_token = encode_base64url(b"n" * 32)

    async def scalar(_: object) -> object:
        events.append("user-lock")
        return user

    async def get(_: str) -> str:
        events.append("redis-read")
        return current_token

    with pytest.raises(HTTPException) as caught:
        await require_prepared_account_vault(
            SimpleNamespace(scalar=scalar),
            SimpleNamespace(get=get),
            user,
            lease_token=stale_token,
            revision="1",
            digest=VAULT_DIGEST,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_ACCOUNT_VAULT_LEASE_EXPIRED"}
    assert events == ["user-lock", "redis-read"]


@pytest.mark.asyncio
async def test_claiming_operation_retry_releases_room_lock_before_user_package_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = cast(
        object,
        SimpleNamespace(id=7, origin_domain="alpha.localhost", is_local=True),
    )
    channel = cast(
        Channel,
        SimpleNamespace(
            id=10,
            origin_domain="alpha.localhost",
            type=0,
            encryption_mode="plaintext",
            encryption_state="plaintext",
            encryption_policy_generation=0,
        ),
    )
    access = SimpleNamespace(channel=channel)
    payload = RoomProposalRequest(
        operation_id=OPERATION_ID,
        sender_device_id=DEVICE_ID,
    )
    request_digest = e2ee_api._operation_request_digest(
        "activate", channel, cast(object, user), payload
    )
    operation = SimpleNamespace(
        id=OPERATION_ID,
        authority_domain="alpha.localhost",
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        actor_id=7,
        actor_domain="alpha.localhost",
        sender_device_id=DEVICE_ID,
        kind="activate",
        status="claiming",
        request_digest=request_digest,
        base_policy_generation=0,
        policy_generation=1,
        group_id=GROUP_ID,
        participant_refs=[{"id": "7", "domain": "alpha.localhost"}],
        key_packages=[],
        prepared_response=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[operation, operation]),
        get=AsyncMock(return_value=user),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        e2ee_api,
        "lock_local_channel_mutation",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(e2ee_api, "require_room_policy_authority", AsyncMock())
    monkeypatch.setattr(e2ee_api, "require_active_sender_device", AsyncMock())
    monkeypatch.setattr(e2ee_api, "room_participants", AsyncMock(return_value=[user]))

    async def claim_after_commit(*_: object, **__: object) -> list[dict[str, str]]:
        assert session.commit.await_count == 1
        return [{"device_id": DEVICE_ID, "key_package": "opaque"}]

    monkeypatch.setattr(e2ee_api, "claim_room_key_packages", claim_after_commit)

    response = await e2ee_api._propose_room_operation(
        "activate",
        access,
        payload,
        SimpleNamespace(user=user),
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="alpha.localhost"),
    )

    assert response["status"] == "prepared"
    assert session.commit.await_count == 2


@pytest.mark.asyncio
async def test_signed_control_metadata_marks_paired_commit_audit_only() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    message = cast(
        Message,
        SimpleNamespace(
            id=100,
            origin_domain="alpha.localhost",
            channel_id=10,
            channel_domain="alpha.localhost",
            author_id=7,
            author_domain="beta.localhost",
            encryption_policy_generation=2,
            encryption_epoch=1,
            e2ee={"operation": "commit", "ciphertext": "opaque"},
            created_at=datetime(2026, 8, 18, tzinfo=UTC),
        ),
    )
    await apply_e2ee_control_metadata(
        session,
        message,
        {
            "operation_id": OPERATION_ID,
            "operation_domain": "alpha.localhost",
            "apply": False,
        },
        expected_authority="alpha.localhost",
    )
    record = cast(E2EEControlRecord, session.add.call_args.args[0])
    assert record.apply_mode == "audit"
    assert record.room_operation_id == OPERATION_ID


@pytest.mark.asyncio
async def test_operation_control_rejects_missing_or_wrong_authority_metadata() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    message = cast(
        Message,
        SimpleNamespace(e2ee={"operation": "welcome"}),
    )
    with pytest.raises(ValueError, match="metadata"):
        await apply_e2ee_control_metadata(
            session,
            message,
            None,
            expected_authority="alpha.localhost",
        )
    with pytest.raises(ValueError, match="metadata"):
        await apply_e2ee_control_metadata(
            session,
            message,
            {
                "operation_id": OPERATION_ID,
                "operation_domain": "beta.localhost",
                "apply": True,
            },
            expected_authority="alpha.localhost",
        )
