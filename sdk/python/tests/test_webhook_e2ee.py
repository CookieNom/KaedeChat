from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.client import Client
from kaede_bot.e2ee import (
    MLS_SUITE,
    E2EEProtocolError,
    WebhookE2EEDevice,
    webhook_device_protocol_id,
    webhook_key_package_upload_input,
    webhook_mls_credential,
)
from kaede_bot.models import Webhook
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class WebhookProvider:
    def __init__(self) -> None:
        self.identity_key = b"w" * 32
        self.signed: list[bytes] = []
        self.package_number = 0
        self.inspected: dict[bytes, tuple[bytes, bytes]] = {}
        self.created_groups: list[bytes] = []
        self.added_packages: list[bytes] = []
        self.merged_groups: list[bytes] = []

    def export_state(self) -> bytes:
        return b"state"

    def public_identity_key(self) -> bytes:
        return self.identity_key

    def sign(self, value: bytes) -> bytes:
        self.signed.append(value)
        return b"s" * 64

    def generate_key_package(self) -> bytes:
        self.package_number += 1
        return f"webhook-package-{self.package_number}".encode()

    def inspect_key_package(self, package: bytes) -> tuple[bytes, bytes]:
        return self.inspected.get(package, (package, self.identity_key))

    def create_group(self, group_id: bytes) -> None:
        self.created_groups.append(group_id)

    def add_members(
        self, group_id: bytes, packages: Sequence[bytes]
    ) -> tuple[bytes, bytes]:
        self.added_packages.extend(packages)
        return b"commit", b"welcome"

    def remove_accounts(
        self, group_id: bytes, accounts: Sequence[str]
    ) -> tuple[bytes, bytes]:
        return b"commit", b""

    def merge_pending_commit(self, group_id: bytes) -> None:
        self.merged_groups.append(group_id)

    def join_group(self, welcome: bytes) -> bytes:
        return b"group"

    def encrypt(self, group_id: bytes, plaintext: bytes, aad: bytes) -> bytes:
        return plaintext

    def process(self, group_id: bytes, message: bytes) -> dict[str, object]:
        return {"kind": "application", "plaintext": b64(message)}

    def group_epoch(self, group_id: bytes) -> int:
        return 1

    def export_epoch_secret(
        self, group_id: bytes, label: str, context: bytes, length: int
    ) -> bytes:
        return b"k" * length

    def close(self) -> None:
        return None


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "webhook-e2ee",
        )
    )


def device_payload(*, available: int = 0) -> dict[str, object]:
    webhook_ref = EntityRef(7, "guild.example")
    identity_key = b"w" * 32
    return {
        "webhook_ref": str(webhook_ref),
        "author_ref": "9@remote-user.example",
        "device_id": webhook_device_protocol_id(webhook_ref, identity_key),
        "identity_key": b64(identity_key),
        "credential": b64(webhook_mls_credential(webhook_ref, identity_key)),
        "capabilities": ["e2ee-media/1", "e2ee-mls/1"],
        "generation": "3",
        "trust_state": "trusted",
        "available_key_packages": available,
    }


def test_webhook_credential_and_device_projection_are_exactly_bound() -> None:
    device = WebhookE2EEDevice.from_payload(device_payload())

    assert device.webhook_ref == EntityRef(7, "guild.example")
    assert device.author_ref == EntityRef(9, "remote-user.example")
    assert device.protocol_id.startswith("kwe_")
    assert device.credential == webhook_mls_credential(
        device.webhook_ref, device.identity_key
    )

    tampered = device_payload()
    tampered["webhook_ref"] = "8@guild.example"
    with pytest.raises(E2EEProtocolError, match="credential identity"):
        WebhookE2EEDevice.from_payload(tampered)


@pytest.mark.asyncio
async def test_registration_and_key_packages_prove_one_token_scoped_device() -> None:
    bot = client()
    provider = WebhookProvider()
    webhook_ref = EntityRef(7, "guild.example")
    challenge_input = b"webhook registration challenge"
    expiry = datetime.now(UTC) + timedelta(days=2)
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "challenge_id": "kwec_" + "c" * 32,
                "signing_input": b64(challenge_input),
                "expires_in": 300,
                "webhook_ref": str(webhook_ref),
            },
            device_payload(),
            {
                "device_id": device_payload()["device_id"],
                "accepted": 2,
                "available_key_packages": 2,
            },
        ]
    )

    device = await bot.register_webhook_e2ee_device(webhook_ref, "secret", provider)
    result = await bot.upload_webhook_e2ee_key_packages(
        webhook_ref,
        "secret",
        provider,
        device,
        count=2,
        expires_at=expiry,
    )

    assert provider.signed[0] == challenge_input
    packages = [b"webhook-package-1", b"webhook-package-2"]
    assert provider.signed[1] == webhook_key_package_upload_input(
        protocol_id=device.protocol_id,
        generation=3,
        cipher_suite=MLS_SUITE,
        expires_at=expiry,
        package_hashes=(hashlib.sha256(item).digest() for item in packages),
    )
    assert result.accepted == 2
    assert all(
        call.kwargs["target"] == "https://guild.example"
        for call in bot.request.await_args_list
    )


@pytest.mark.asyncio
async def test_encrypted_webhook_send_edit_and_attachment_bind_exact_device() -> None:
    bot = client()
    device_id = str(device_payload()["device_id"])
    channel = EntityRef(13, "guild.example")
    envelope = {
        "version": 2,
        "protocol": "mls10",
        "sender_device_id": device_id,
        "ciphertext": "opaque",
    }
    rendered = {
        "id": "20",
        "origin_domain": "guild.example",
        "channel_id": "13",
        "channel_domain": "guild.example",
        "webhook_id": "7",
        "webhook_domain": "guild.example",
        "content": None,
        "attachments": [],
        "e2ee": envelope,
    }
    ticket = {
        "id": "30",
        "origin_domain": "guild.example",
        "filename": "secret.bin",
        "content_type": "application/octet-stream",
        "size": 10,
        "encryption_mode": "e2ee",
        "encryption_protocol": "kaede-file-v1",
        "upload_url": "https://upload.example/ticket",
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[rendered, rendered, rendered, ticket]
    )
    bot._put_upload_ticket = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001

    created = await bot.execute_webhook(
        7,
        "secret",
        target="https://guild.example",
        e2ee=envelope,
        e2ee_device_id=device_id,
    )
    assert created is not None
    edited = await created.edit(e2ee=envelope)
    await edited.edit(e2ee=envelope)
    await bot.upload_webhook_attachment(
        7,
        "secret",
        b"ciphertext",
        filename="secret.bin",
        content_type="application/octet-stream",
        target="https://guild.example",
        channel_ref=channel,
        e2ee_device_id=device_id,
        encryption_protocol="kaede-file-v1",
    )

    create, edit, repeated_edit, upload = bot.request.await_args_list
    assert create.kwargs["headers"] == {"X-Kaede-E2EE-Device": device_id}
    assert create.kwargs["json"]["content"] is None
    assert create.kwargs["json"]["e2ee"] == envelope
    assert edit.kwargs["headers"] == {"X-Kaede-E2EE-Device": device_id}
    assert edit.kwargs["json"] == {"e2ee": envelope}
    assert repeated_edit.kwargs["headers"] == {"X-Kaede-E2EE-Device": device_id}
    assert repeated_edit.kwargs["json"] == {"e2ee": envelope}
    assert upload.kwargs["params"] == {"channel_id": str(channel)}
    assert upload.kwargs["headers"] == {"X-Kaede-E2EE-Device": device_id}
    assert upload.kwargs["json"]["encryption_mode"] == "e2ee"


@pytest.mark.asyncio
async def test_manager_participation_routes_to_remote_guild_authority() -> None:
    bot = client()
    guild = EntityRef(11, "guild.example")
    webhook = EntityRef(7, "guild.example")
    channel = EntityRef(13, "guild.example")
    response = {
        "webhook_ref": str(webhook),
        "channel_ref": str(channel),
        "devices": [],
        "encryption_policy": {"mode": "e2ee"},
    }
    bot.request = AsyncMock(return_value=response)  # type: ignore[method-assign]

    status = await bot.set_webhook_e2ee_participation(
        guild, webhook, channel, True, reason="automation consent"
    )

    assert status.webhook_ref == webhook
    bot.request.assert_awaited_once_with(
        "PUT",
        f"/api/v1/bots/guilds/{guild}/webhooks/7/e2ee/channels/{channel}",
        target="https://guild.example",
        headers={"X-Audit-Log-Reason": "automation consent"},
    )


@pytest.mark.asyncio
async def test_webhook_e2ee_rejects_cross_authority_refs_before_request() -> None:
    bot = client()
    guild = EntityRef(11, "guild.example")
    webhook = EntityRef(7, "guild.example")
    foreign_channel = EntityRef(13, "other.example")
    bot.request = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="webhook E2EE resource"):
        await bot.webhook_e2ee_participation(guild, webhook, foreign_channel)
    with pytest.raises(ValueError, match="webhook E2EE resource"):
        await bot.set_webhook_e2ee_participation(
            guild,
            webhook,
            foreign_channel,
            True,
        )
    with pytest.raises(ValueError, match="webhook E2EE resource"):
        await bot.fetch_webhook_e2ee_control_log(
            webhook,
            "secret",
            foreign_channel,
            "kwe_" + "d" * 43,
        )
    with pytest.raises(ValueError, match="webhook E2EE resource"):
        await bot.propose_webhook_encrypted_forum_room(
            webhook,
            "secret",
            foreign_channel,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            "keo_" + "o" * 43,
        )
    with pytest.raises(ValueError, match="webhook E2EE resource"):
        await bot.activate_webhook_encrypted_forum_room(
            webhook,
            "secret",
            foreign_channel,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            commit=b"commit",
            welcome=b"welcome",
        )
    with pytest.raises(ValueError, match="webhook E2EE resource"):
        await bot.claim_webhook_encrypted_forum_starter(
            webhook,
            "secret",
            foreign_channel,
            "kwe_" + "d" * 43,
            client_nonce="delivery-1",
            e2ee={"ciphertext": "opaque"},
        )

    bot.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_encrypted_forum_lifecycle_is_authority_and_device_bound() -> (
    None
):
    bot = client()
    provider = WebhookProvider()
    webhook_ref = EntityRef(7, "guild.example")
    channel_ref = EntityRef(13, "guild.example")
    thread_ref = EntityRef(21, "guild.example")
    device = WebhookE2EEDevice.from_payload(device_payload())
    nonce = "delivery-1"
    operation_digest = hashlib.sha256(
        (
            f"kaede-webhook-forum-operation-v1\0{webhook_ref}\0{thread_ref}\0"
            f"{device.protocol_id}\0{nonce}"
        ).encode()
    ).digest()
    operation_id = "keo_" + b64(operation_digest)
    group_id = b"g" * 32
    human_credential = json.dumps(
        {
            "version": 1,
            "account": "9@guild.example",
            "nonce": "n" * 43,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    human_package = b"human-package"
    human_identity = b"h" * 32
    provider.inspected[human_package] = (human_credential, human_identity)
    reservation = {
        "id": "21",
        "origin_domain": "guild.example",
        "guild_id": "11",
        "guild_domain": "guild.example",
        "parent_id": "13",
        "parent_domain": "guild.example",
        "type": 11,
        "name": "Encrypted topic",
        "e2ee_required": True,
        "encryption_mode": "plaintext",
        "encryption_state": "plaintext",
        "starter_reservation": {"client_nonce": nonce, "claimed": False},
        "starter_message": None,
        "webhook_e2ee": {"device_id": device.protocol_id, "status": "pending"},
    }
    proposal = {
        "operation_id": operation_id,
        "status": "prepared",
        "policy": {
            "mode": "plaintext",
            "state": "proposed",
            "generation": "1",
            "protocol": "mls10",
            "suite": MLS_SUITE,
            "group_id": b64(group_id),
            "epoch": None,
        },
        "key_packages": [
            {
                "user_id": "9",
                "user_domain": "guild.example",
                "device_id": "ked_" + "h" * 43,
                "identity_key": b64(human_identity),
                "credential": b64(human_credential),
                "key_package": b64(human_package),
            }
        ],
    }
    activation = reservation | {
        "encryption_mode": "e2ee",
        "encryption_state": "active",
        "encryption_policy_generation": "1",
        "encryption_group_id": b64(group_id),
        "encryption_epoch": "1",
        "operation_id": operation_id,
        "operation_status": "committed",
    }
    message_payload = {
        "id": "21",
        "origin_domain": "guild.example",
        "channel_id": "21",
        "channel_domain": "guild.example",
        "webhook_id": "7",
        "webhook_domain": "guild.example",
        "content": None,
        "attachments": [],
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"webhook_ref": str(webhook_ref), "devices": [device_payload()]},
            reservation,
            proposal,
            activation,
            message_payload,
        ]
    )
    item = Webhook(
        client=bot,
        target="https://gateway.example",
        ref=webhook_ref,
        guild_ref=EntityRef(11, "guild.example"),
        channel_ref=channel_ref,
        name="Website",
        token="secret",
    )
    item.set_e2ee_device(device)
    rich = {
        "content": "encrypted starter",
        "embeds": [],
        "components": [],
        "poll": None,
        "sticker_items": [],
        "tts": False,
        "voice_message": False,
        "flags": 0,
        "attachments": [],
        "allowed_mentions": {
            "parse": [],
            "users": [],
            "roles": [],
            "replied_user": False,
        },
        "forward_snapshot": None,
    }

    message, context = await item.create_encrypted_forum_post(
        provider,
        "Encrypted topic",
        rich,
        client_nonce=nonce,
    )

    assert message.ref == thread_ref
    assert context.channel_ref == thread_ref
    assert provider.created_groups == [group_id]
    assert provider.added_packages == [human_package]
    assert provider.merged_groups == [group_id]
    calls = bot.request.await_args_list
    assert all(call.kwargs["target"] == "https://guild.example" for call in calls)
    assert calls[1].kwargs["headers"] == {"X-Kaede-E2EE-Device": device.protocol_id}
    assert calls[2].kwargs["json"]["operation_id"] == operation_id
    assert calls[3].kwargs["json"]["prepared_vault_revision"] == "3"
    assert calls[4].kwargs["json"]["client_nonce"] == nonce
    assert calls[4].kwargs["json"]["e2ee"]["operation"] == "create"
