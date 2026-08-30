from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api.channels import require_owned_e2ee_sender_device
from app.api.e2ee import RoomActivationRequest
from app.api.webhook_e2ee import (
    WebhookEncryptedForumReservationRequest,
    activate_webhook_encrypted_forum_room,
    create_webhook_encrypted_forum_reservation,
    render_webhook_e2ee_device,
    webhook_device_protocol_id,
    webhook_mls_credential,
)
from app.api.webhooks import delete_webhook_with_token
from app.chat.e2ee import (
    E2EE_PROTOCOL_MLS_10,
    E2EE_SUITE_MLS_128,
    validate_e2ee_envelope,
)
from app.core.types import EntityRef


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def webhook() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        guild_id=11,
        guild_domain="guild.example",
        channel_id=13,
        channel_domain="guild.example",
        creator_id=9,
        creator_domain="remote-user.example",
        revoked_at=None,
    )


def test_webhook_device_credential_uses_a_distinct_exact_namespace() -> None:
    item = webhook()
    identity_key = b"w" * 32
    device_id = webhook_device_protocol_id(item, identity_key)  # type: ignore[arg-type]
    credential = webhook_mls_credential(item, device_id)  # type: ignore[arg-type]

    assert device_id == "kwe_" + b64(
        hashlib.sha256(
            b"kaede-webhook-e2ee-device-v1\x007@guild.example\x00" + identity_key
        ).digest()
    )
    assert json.loads(credential) == {
        "account": "webhook:7@guild.example",
        "credential_type": "kaede-webhook-device-v1",
        "device_id": device_id,
        "webhook_ref": "7@guild.example",
    }


def test_shared_mls_envelope_accepts_webhook_devices_without_weakening_shape() -> None:
    envelope = {
        "version": 2,
        "protocol": E2EE_PROTOCOL_MLS_10,
        "suite": E2EE_SUITE_MLS_128,
        "group_id": b64(b"group"),
        "policy_generation": "1",
        "epoch": "1",
        "sender_device_id": "kwe_" + "a" * 43,
        "operation": "create",
        "ciphertext": b64(b"ciphertext"),
    }

    assert validate_e2ee_envelope(envelope) == envelope
    envelope["sender_device_id"] = "webhook-7"
    with pytest.raises(ValueError, match="sender device"):
        validate_e2ee_envelope(envelope)


@pytest.mark.asyncio
async def test_webhook_sender_admission_requires_exact_header_and_participation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = webhook()
    channel = SimpleNamespace(id=13, origin_domain="guild.example")
    session = SimpleNamespace(get=AsyncMock(return_value=item))
    participant = AsyncMock()
    monkeypatch.setattr("app.api.webhook_e2ee.require_webhook_e2ee_participation", participant)
    device_id = "kwe_" + "a" * 43

    await require_owned_e2ee_sender_device(
        session,  # type: ignore[arg-type]
        SimpleNamespace(account_type="human"),  # type: ignore[arg-type]
        {"sender_device_id": device_id},
        authority_domain="guild.example",
        channel=channel,  # type: ignore[arg-type]
        webhook_id=7,
        webhook_domain="guild.example",
        webhook_e2ee_device_id=device_id,
    )
    participant.assert_awaited_once_with(session, item, channel, device_id)

    with pytest.raises(HTTPException) as mismatch:
        await require_owned_e2ee_sender_device(
            session,  # type: ignore[arg-type]
            SimpleNamespace(account_type="human"),  # type: ignore[arg-type]
            {"sender_device_id": device_id},
            authority_domain="guild.example",
            channel=channel,  # type: ignore[arg-type]
            webhook_id=7,
            webhook_domain="guild.example",
            webhook_e2ee_device_id="kwe_" + "b" * 43,
        )
    assert mismatch.value.detail["code"] == "WEBHOOK_E2EE_PARTICIPANT_REQUIRED"


def test_device_projection_binds_webhook_and_storage_author_without_token() -> None:
    item = webhook()
    device = SimpleNamespace(
        webhook_id=7,
        webhook_domain="guild.example",
        protocol_id="kwe_" + "a" * 43,
        identity_key=b"w" * 32,
        credential=b"credential",
        capabilities=["e2ee-mls/1"],
        generation=2,
        trust_state="trusted",
    )

    rendered = render_webhook_e2ee_device(
        device,  # type: ignore[arg-type]
        item,  # type: ignore[arg-type]
        available_key_packages=4,
    )

    assert rendered["webhook_ref"] == "7@guild.example"
    assert rendered["author_ref"] == "9@remote-user.example"
    assert "token" not in rendered


@pytest.mark.asyncio
async def test_token_delete_revokes_mls_access_and_rekeys_before_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = webhook()
    creator = SimpleNamespace(
        id=9,
        origin_domain="remote-user.example",
        account_type="human",
        is_local=False,
    )
    guild = SimpleNamespace(id=11, origin_domain="guild.example")
    channel = SimpleNamespace(id=13, origin_domain="guild.example")
    session = SimpleNamespace(
        get=AsyncMock(return_value=creator),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace()
    revoke = AsyncMock(return_value=(guild, [channel]))
    publish_revoke = AsyncMock()
    publish_update = AsyncMock()
    settings = SimpleNamespace(domain="guild.example")
    monkeypatch.setattr("app.api.webhooks.token_webhook", AsyncMock(return_value=item))
    monkeypatch.setattr("app.api.webhook_e2ee.revoke_webhook_e2ee_access", revoke)
    monkeypatch.setattr("app.api.webhook_e2ee.publish_webhook_e2ee_revocation", publish_revoke)
    monkeypatch.setattr("app.api.webhooks.publish_webhook_update", publish_update)

    response = await delete_webhook_with_token(
        7,
        "secret",
        session=session,  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
    )

    assert response.status_code == 204
    assert item.revoked_at is not None and item.revoked_at.tzinfo == UTC
    revoke.assert_awaited_once_with(
        session,
        settings,
        item,
        creator,
    )
    publish_revoke.assert_awaited_once_with(session, redis, guild, [channel])
    publish_update.assert_awaited_once_with(redis, item)


@pytest.mark.asyncio
async def test_webhook_forum_reservation_binds_and_clones_exact_device_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = webhook()
    item.type = 1
    item.application_id = None
    item.application_domain = None
    item.name = "Website"
    item.avatar_hash = "avatar"
    parent = SimpleNamespace(
        id=13,
        origin_domain="guild.example",
        type=15,
        e2ee_required=True,
    )
    creator = SimpleNamespace(
        id=9,
        origin_domain="remote-user.example",
        account_type="human",
        is_local=False,
    )
    thread = SimpleNamespace(id=21, origin_domain="guild.example")
    device = SimpleNamespace(id=31, protocol_id="kwe_" + "a" * 43)
    parent_participation = SimpleNamespace(
        consenting_actor_id=4,
        consenting_actor_domain="guild.example",
        consent_generation=3,
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[creator, thread]),
        scalar=AsyncMock(return_value=None),
        add=Mock(),
        commit=AsyncMock(),
    )
    create_thread = AsyncMock(
        return_value={
            "id": "21",
            "origin_domain": "guild.example",
            "starter_reservation": {"client_nonce": "delivery-1", "claimed": False},
        }
    )
    monkeypatch.setattr("app.api.webhook_e2ee.token_webhook", AsyncMock(return_value=item))
    monkeypatch.setattr(
        "app.api.webhook_e2ee.webhook_e2ee_target_channel",
        AsyncMock(return_value=parent),
    )
    monkeypatch.setattr(
        "app.api.webhook_e2ee.require_webhook_e2ee_participation",
        AsyncMock(return_value=(parent_participation, device)),
    )
    monkeypatch.setattr("app.api.threads.create_thread_service", create_thread)
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=41))

    rendered = await create_webhook_encrypted_forum_reservation(
        7,
        "secret",
        WebhookEncryptedForumReservationRequest(
            name="Encrypted topic",
            applied_tag_ids=["5"],
            client_nonce="delivery-1",
        ),
        device.protocol_id,
        session=session,  # type: ignore[arg-type]
        redis=SimpleNamespace(),  # type: ignore[arg-type]
        snowflake=snowflake,  # type: ignore[arg-type]
        settings=SimpleNamespace(domain="guild.example"),  # type: ignore[arg-type]
    )

    assert rendered["webhook_e2ee"] == {
        "device_id": device.protocol_id,
        "status": "pending",
    }
    created_participation = session.add.call_args.args[0]
    assert created_participation.webhook_id == 7
    assert created_participation.channel_id == 21
    assert created_participation.device_id == 31
    assert created_participation.consent_generation == 3
    options = create_thread.await_args.kwargs["starter_admission_options"]
    assert options.webhook_id == 7
    assert options.webhook_e2ee_device_id == device.protocol_id
    assert create_thread.await_args.kwargs["starter_claimant_device_id"] == device.protocol_id


@pytest.mark.asyncio
async def test_webhook_forum_activation_fences_device_generation_and_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = webhook()
    creator = SimpleNamespace(id=9, origin_domain="remote-user.example")
    access = SimpleNamespace(channel=SimpleNamespace(id=21, origin_domain="guild.example"))
    device = SimpleNamespace(
        protocol_id="kwe_" + "a" * 43,
        generation=4,
        credential=b"webhook credential",
    )
    activate = AsyncMock(return_value={"operation_status": "committed"})
    monkeypatch.setattr("app.api.webhook_e2ee.token_webhook", AsyncMock(return_value=item))
    monkeypatch.setattr(
        "app.api.webhook_e2ee._webhook_forum_reservation_access",
        AsyncMock(return_value=(access, SimpleNamespace(), SimpleNamespace(), device, creator)),
    )
    monkeypatch.setattr("app.api.webhook_e2ee.activate_automation_room_encryption", activate)
    payload = RoomActivationRequest(
        operation_id="keo_" + "o" * 43,
        sender_device_id=device.protocol_id,
        policy_generation="1",
        epoch="1",
        group_id=b64(b"g" * 32),
        commit=b64(b"commit"),
        welcome=b64(b"welcome"),
        prepared_vault_revision="4",
        prepared_vault_digest=b64(hashlib.sha256(device.credential).digest()),
    )

    rendered = await activate_webhook_encrypted_forum_room(
        7,
        "secret",
        EntityRef("21@guild.example"),
        payload,
        device.protocol_id,
        session=SimpleNamespace(),  # type: ignore[arg-type]
        redis=SimpleNamespace(),  # type: ignore[arg-type]
        snowflake=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(domain="guild.example"),  # type: ignore[arg-type]
    )

    assert rendered == {"operation_status": "committed"}
    activate.assert_awaited_once()

    tampered = payload.model_copy(update={"prepared_vault_revision": "5"})
    with pytest.raises(HTTPException) as mismatch:
        await activate_webhook_encrypted_forum_room(
            7,
            "secret",
            EntityRef("21@guild.example"),
            tampered,
            device.protocol_id,
            session=SimpleNamespace(),  # type: ignore[arg-type]
            redis=SimpleNamespace(),  # type: ignore[arg-type]
            snowflake=SimpleNamespace(),  # type: ignore[arg-type]
            settings=SimpleNamespace(domain="guild.example"),  # type: ignore[arg-type]
        )
    assert mismatch.value.detail["code"] == "WEBHOOK_E2EE_DEVICE_STATE_MISMATCH"
