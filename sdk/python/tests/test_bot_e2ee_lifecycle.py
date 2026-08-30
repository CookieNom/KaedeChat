from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import kaede_bot.client as client_module
from kaede_bot.client import Client
from kaede_bot.e2ee import (
    MLS_SUITE,
    BotE2EEControlPage,
    BotE2EEControlRecord,
    E2EEProvider,
    E2EEProtocolError,
    InteractionE2EEContext,
    bot_device_protocol_id,
    bot_key_package_upload_input,
    bot_mls_credential,
)
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class LifecycleProvider:
    def __init__(self) -> None:
        self.identity_key = b"i" * 32
        self.signed: list[bytes] = []
        self.package_number = 0

    def export_state(self) -> bytes:
        return b"state"

    def public_identity_key(self) -> bytes:
        return self.identity_key

    def sign(self, value: bytes) -> bytes:
        self.signed.append(value)
        return b"s" * 64

    def generate_key_package(self) -> bytes:
        self.package_number += 1
        return f"package-{self.package_number}".encode()

    def inspect_key_package(self, package: bytes) -> tuple[bytes, bytes]:
        return package, self.identity_key

    def create_group(self, group_id: bytes) -> None:
        return None

    def add_members(
        self, group_id: bytes, packages: Sequence[bytes]
    ) -> tuple[bytes, bytes]:
        return b"commit", b"welcome"

    def remove_accounts(
        self, group_id: bytes, accounts: Sequence[str]
    ) -> tuple[bytes, bytes]:
        return b"commit", b""

    def merge_pending_commit(self, group_id: bytes) -> None:
        return None

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


def lifecycle_client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "e2ee",
        )
    )


def device_payload(*, available: int = 0) -> dict[str, object]:
    application_ref = EntityRef(1, "apps.example")
    identity_key = b"i" * 32
    return {
        "source_id": "9",
        "source_domain": "apps.example",
        "protocol_id": bot_device_protocol_id(application_ref, 2, identity_key),
        "worker_id": "2",
        "identity_key": b64(identity_key),
        "credential": b64(bot_mls_credential(application_ref, 2, identity_key)),
        "capabilities": ["e2ee-media/1", "e2ee-mls/1"],
        "generation": "3",
        "trust_state": "trusted",
        "available_key_packages": available,
    }


def control_payload(
    control_id: int,
    *,
    operation: str,
    epoch: int,
) -> dict[str, object]:
    return {
        "id": str(control_id),
        "origin_domain": "chat.example",
        "channel_id": "5",
        "channel_domain": "chat.example",
        "author_id": "10",
        "author_domain": "chat.example",
        "room_operation_id": "keo_" + "o" * 43,
        "room_operation_domain": "chat.example",
        "apply": True,
        "encryption_policy_generation": "1",
        "encryption_epoch": str(epoch),
        "e2ee": {
            "version": 2,
            "protocol": "mls10",
            "suite": MLS_SUITE,
            "operation": operation,
            "policy_generation": "1",
            "epoch": str(epoch),
            "sender_device_id": "kbe_" + "d" * 43,
            "group_id": b64(b"group"),
            "ciphertext": b64(operation.encode()),
        },
    }


class RecoveryProvider(LifecycleProvider):
    def __init__(self, *, fail_commit: bool = False) -> None:
        super().__init__()
        self.epochs: dict[bytes, int] = {}
        self.fail_commit = fail_commit

    def export_state(self) -> bytes:
        return repr(sorted(self.epochs.items())).encode()

    def join_group(self, welcome: bytes) -> bytes:
        assert welcome == b"welcome"
        self.epochs[b"group"] = 1
        return b"group"

    def group_epoch(self, group_id: bytes) -> int:
        try:
            return self.epochs[group_id]
        except KeyError as exc:
            raise E2EEProtocolError("unknown group") from exc

    def process(self, group_id: bytes, message: bytes) -> dict[str, object]:
        assert message == b"commit"
        if self.fail_commit:
            raise E2EEProtocolError("commit rejected")
        self.epochs[group_id] += 1
        return {"kind": "commit"}


@pytest.mark.asyncio
async def test_bot_identity_is_typed_and_application_home_routed() -> None:
    bot = lifecycle_client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "user": {
                "id": "10",
                "origin_domain": "apps.example",
                "username": "weather",
                "bot": True,
                "account_type": "bot",
            },
            "application_ref": "1@apps.example",
            "worker_id": "2",
            "scopes": ["dm.send"],
            "intents": ["direct_messages"],
            "token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )

    identity = await bot.fetch_bot_identity()

    assert identity.user.bot is True
    assert identity.application_ref == bot.worker_state.application_ref
    assert identity.scopes == frozenset({"dm.send"})
    bot.request.assert_awaited_once_with("GET", "/api/v1/bots/@me", target=None)
    assert bot._is_application_home_path("/api/v1/bots/@me")  # noqa: SLF001
    assert bot._is_application_home_path(  # noqa: SLF001
        "/api/v1/bots/e2ee/devices/challenge"
    )


@pytest.mark.asyncio
async def test_device_registration_proves_provider_identity() -> None:
    bot = lifecycle_client()
    provider = LifecycleProvider()
    signing_input = b"registration challenge"
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "challenge_id": "kbec_" + "c" * 32,
                "signing_input": b64(signing_input),
                "expires_in": 300,
                "application_ref": "1@apps.example",
                "worker_id": "2",
                "domain": "apps.example",
            },
            device_payload(),
        ]
    )

    device = await bot.register_e2ee_device(provider)

    assert isinstance(provider, E2EEProvider)
    assert device.protocol_id == bot_device_protocol_id(
        bot.worker_state.application_ref,
        2,
        provider.identity_key,
    )
    assert provider.signed == [signing_input]
    challenge_request, register_request = bot.request.await_args_list
    assert challenge_request.args == (
        "POST",
        "/api/v1/bots/e2ee/devices/challenge",
    )
    assert challenge_request.kwargs["json"] == {
        "identity_key": b64(provider.identity_key),
        "credential_digest": b64(
            hashlib.sha256(
                bot_mls_credential(
                    bot.worker_state.application_ref,
                    2,
                    provider.identity_key,
                )
            ).digest()
        ),
    }
    assert register_request.args == ("POST", "/api/v1/bots/e2ee/devices")
    assert register_request.kwargs["json"]["signature"] == b64(b"s" * 64)


@pytest.mark.asyncio
async def test_key_package_replenishment_signs_the_exact_generation() -> None:
    bot = lifecycle_client()
    provider = LifecycleProvider()
    expiry = datetime.now(UTC) + timedelta(days=2)
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"generation": "3", "devices": [device_payload(available=1)]},
            {
                "device_id": device_payload()["protocol_id"],
                "accepted": 4,
                "available_key_packages": 5,
            },
        ]
    )

    device = await bot.replenish_e2ee_key_packages(
        provider,
        minimum_available=2,
        desired_available=5,
        expires_at=expiry,
    )

    assert device.available_key_packages == 5
    assert provider.package_number == 4
    packages = [f"package-{number}".encode() for number in range(1, 5)]
    assert provider.signed == [
        bot_key_package_upload_input(
            protocol_id=device.protocol_id,
            generation=device.generation,
            cipher_suite=MLS_SUITE,
            expires_at=expiry,
            package_hashes=(hashlib.sha256(item).digest() for item in packages),
        )
    ]
    upload = bot.request.await_args_list[1]
    assert upload.args == (
        "POST",
        f"/api/v1/bots/e2ee/devices/{device.protocol_id}/key-packages",
    )
    assert upload.kwargs["json"]["packages"] == [b64(item) for item in packages]


@pytest.mark.asyncio
async def test_revoke_and_participation_use_exact_runtime_grant() -> None:
    bot = lifecycle_client()
    channel = EntityRef(5, "chat.example")
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            None,
            {
                "application_ref": "1@apps.example",
                "channel_ref": str(channel),
                "e2ee_mode": "participant",
                "devices": [
                    {
                        "device_id": "kbe_" + "d" * 43,
                        "status": "active",
                        "consent_generation": "2",
                        "joined_epoch": "7",
                        "history_floor_message_ref": "99@chat.example",
                    }
                ],
                "encryption_policy": {
                    "mode": "e2ee",
                    "state": "active",
                    "generation": "4",
                    "epoch": "7",
                },
            },
        ]
    )

    await bot.revoke_e2ee_device("kbe_" + "d" * 43)
    status = await bot.e2ee_participation(channel, installation_id=77)

    assert status.channel_ref == channel
    assert status.devices[0].joined_epoch == 7
    assert status.devices[0].history_floor_message_ref == EntityRef(99, "chat.example")
    participation = bot.request.await_args_list[1]
    assert participation.kwargs["headers"] == {"X-Kaede-Bot-Installation": "77"}
    assert participation.kwargs["target"] == "https://chat.example"


def test_bound_worker_assertion_is_signed_for_one_grant_revision_and_audience() -> None:
    bot = lifecycle_client()
    context = client_module._DMCapabilityContext(  # noqa: SLF001
        installation_ref=EntityRef(77, "guild.example"),
        installation_type="guild",
        grant_id="kbdg_" + "g" * 43,
        revision=6,
        expires_at=datetime.now(UTC).timestamp() + 600,
        target="https://chat.example",
    )

    assertion = bot._worker_assertion(  # noqa: SLF001
        "https://chat.example",
        "/api/v1/bots/token",
        dm_capability=context,
    )

    signed = (
        f"kaede-worker-assertion-v2\n1@apps.example\n2\n"
        f"{assertion['audience']}\n{assertion['issued_at']}\n"
        f"{assertion['expires_at']}\n{assertion['nonce']}\n"
        f"{context.grant_id}\n{context.revision}"
    ).encode()
    signature = base64.urlsafe_b64decode(
        str(assertion["signature"]) + "=" * (-len(str(assertion["signature"])) % 4)
    )
    bot.worker_state.private_key.public_key().verify(signature, signed)
    assert assertion["dm_capability_grant_id"] == context.grant_id
    assert assertion["dm_capability_revision"] == context.revision


@pytest.mark.asyncio
async def test_restart_bootstrap_refreshes_opaque_grant_without_a_handle() -> None:
    bot = lifecycle_client()
    channel = {
        "id": "5",
        "origin_domain": "chat.example",
        "type": 1,
        "bot_dm_capability_id": "kbdg_" + "g" * 43,
        "bot_dm_capability_revision": "4",
        "bot_installation_ref": "77@guild.example",
        "bot_installation_type": "guild",
    }
    item: dict[str, Any] = {
        "grant_id": "kbdg_" + "g" * 43,
        "revision": "4",
        "authority_origin": "https://chat.example",
        "channel_ref": "5@chat.example",
        "installation_ref": "77@guild.example",
        "installation_type": "guild",
        "lineage_ref": "99@chat.example",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "channel": channel,
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{"items": [item], "next_after": None}, item]
    )

    await bot._bootstrap_dm_capabilities()  # noqa: SLF001

    key = (EntityRef(5, "chat.example"), "kbdg_" + "g" * 43)
    assert key in bot._dm_capabilities  # noqa: SLF001
    assert not hasattr(bot._dm_capabilities[key], "handle")  # noqa: SLF001
    refresh = bot.request.await_args_list[1]
    assert refresh.args == (
        "POST",
        "/api/v1/bots/dm-capabilities/kbdg_ggggggggggggggggggggggggggggggggggggggggggg/refresh",
    )
    assert "handle" not in refresh.kwargs


def test_control_page_requires_strict_authority_order() -> None:
    payload = {
        "application_ref": "1@apps.example",
        "channel_ref": "5@chat.example",
        "device_id": "kbe_" + "d" * 43,
        "controls": [
            control_payload(1, operation="welcome", epoch=1),
            control_payload(2, operation="commit", epoch=2),
        ],
        "next_after": None,
    }

    page = BotE2EEControlPage.from_payload(payload)
    assert [control.ref.id for control in page.controls] == [1, 2]

    payload["controls"] = list(reversed(payload["controls"]))
    with pytest.raises(E2EEProtocolError, match="out of order"):
        BotE2EEControlPage.from_payload(payload)


@pytest.mark.asyncio
async def test_offline_control_sync_applies_welcome_then_commit_and_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = lifecycle_client()
    device_id = "kbe_" + "d" * 43
    bot._e2ee_device_id = device_id  # noqa: SLF001
    provider = RecoveryProvider()
    context = InteractionE2EEContext(
        provider=provider,
        channel_ref=EntityRef(5, "chat.example"),
        group_id=b"group",
        policy_generation=1,
        epoch=0,
    )
    controls = tuple(
        BotE2EEControlRecord.from_payload(payload)
        for payload in (
            control_payload(1, operation="welcome", epoch=1),
            control_payload(2, operation="commit", epoch=2),
        )
    )
    fetch = AsyncMock(
        return_value=BotE2EEControlPage(
            EntityRef(1, "apps.example"),
            context.channel_ref,
            device_id,
            controls,
            None,
        )
    )
    monkeypatch.setattr(bot, "_fetch_e2ee_control_log", fetch)
    save = Mock()
    monkeypatch.setattr(WorkerState, "save_e2ee_control_checkpoints", save)

    cursor = await bot._sync_e2ee_control_log(  # noqa: SLF001
        context,
        headers={"X-Kaede-Bot-Installation": "77"},
        target="https://chat.example",
    )

    assert cursor == "2@chat.example"
    assert context.epoch == 2
    assert provider.group_epoch(b"group") == 2
    assert save.call_count == 2
    checkpoint = next(iter(bot._e2ee_control_checkpoints.values()))  # noqa: SLF001
    assert checkpoint[0] == "2@chat.example"


@pytest.mark.asyncio
async def test_control_sync_never_checkpoints_a_failed_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = lifecycle_client()
    device_id = "kbe_" + "d" * 43
    bot._e2ee_device_id = device_id  # noqa: SLF001
    provider = RecoveryProvider(fail_commit=True)
    context = InteractionE2EEContext(
        provider=provider,
        channel_ref=EntityRef(5, "chat.example"),
        group_id=b"group",
        policy_generation=1,
        epoch=0,
    )
    page = BotE2EEControlPage(
        EntityRef(1, "apps.example"),
        context.channel_ref,
        device_id,
        tuple(
            BotE2EEControlRecord.from_payload(payload)
            for payload in (
                control_payload(1, operation="welcome", epoch=1),
                control_payload(2, operation="commit", epoch=2),
            )
        ),
        None,
    )
    monkeypatch.setattr(bot, "_fetch_e2ee_control_log", AsyncMock(return_value=page))
    save = Mock()
    monkeypatch.setattr(WorkerState, "save_e2ee_control_checkpoints", save)

    with pytest.raises(E2EEProtocolError, match="commit rejected"):
        await bot._sync_e2ee_control_log(  # noqa: SLF001
            context,
            headers={"X-Kaede-Bot-Installation": "77"},
            target="https://chat.example",
        )

    assert context.epoch == 1
    assert save.call_count == 1
    checkpoint = next(iter(bot._e2ee_control_checkpoints.values()))  # noqa: SLF001
    assert checkpoint[0] == "1@chat.example"
