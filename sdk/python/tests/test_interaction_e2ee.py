from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.client import Client
from kaede_bot.e2ee import (
    E2EEProtocolError,
    InteractionE2EEContext,
    decrypt_interaction,
    encrypt_interaction_response,
    interaction_attachment_manifest_digest,
    interaction_authenticated_context,
    interaction_authenticated_data,
    interaction_plaintext,
    interaction_response_authenticated_data,
    interaction_response_authenticated_context,
    interaction_response_plaintext,
    interaction_routing_contract,
    interaction_routing_contract_digest,
)
from kaede_bot.models import Interaction
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class FakeProvider:
    def __init__(self) -> None:
        self.epoch = 7
        self.result: dict[str, object] = {}

    def export_state(self) -> bytes:
        return b"state"

    def public_identity_key(self) -> bytes:
        return b"i" * 32

    def sign(self, value: bytes) -> bytes:
        return value

    def generate_key_package(self) -> bytes:
        return b"package"

    def inspect_key_package(self, package: bytes) -> tuple[bytes, bytes]:
        return package, b"i" * 32

    def create_group(self, group_id: bytes) -> None:
        del group_id

    def add_members(
        self, group_id: bytes, packages: Sequence[bytes]
    ) -> tuple[bytes, bytes]:
        del group_id, packages
        return b"commit", b"welcome"

    def remove_accounts(
        self, group_id: bytes, accounts: Sequence[str]
    ) -> tuple[bytes, bytes]:
        del group_id, accounts
        return b"commit", b""

    def merge_pending_commit(self, group_id: bytes) -> None:
        del group_id

    def join_group(self, welcome: bytes) -> bytes:
        return welcome

    def encrypt(self, group_id: bytes, plaintext: bytes, aad: bytes) -> bytes:
        del group_id, aad
        return plaintext

    def process(self, group_id: bytes, message: bytes) -> dict[str, object]:
        del group_id, message
        return dict(self.result)

    def group_epoch(self, group_id: bytes) -> int:
        del group_id
        return self.epoch

    def export_epoch_secret(
        self, group_id: bytes, label: str, context: bytes, length: int
    ) -> bytes:
        del group_id, label, context
        return b"k" * length

    def close(self) -> None:
        pass


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(10, "apps.example"),
            11,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def interaction_payload(*, interaction_id: int = 90) -> dict[str, Any]:
    return {
        "id": str(interaction_id),
        "interaction_ref": f"{interaction_id}@guild.example",
        "token": b64url(b"t" * 32),
        "expires_at": "2099-01-01T00:00:00+00:00",
        "application_ref": "10@apps.example",
        "guild_ref": "20@guild.example",
        "channel_ref": "30@guild.example",
        "user": {
            "id": "40",
            "origin_domain": "users.example",
            "username": "alice",
            "display_name": "Alice",
        },
        "command": {
            "name": "secure",
            "type": "chat_input",
            "description": "Secure command",
            "options": [
                {
                    "name": "query",
                    "description": "Query",
                    "type": "string",
                    "required": True,
                    "min_length": 2,
                    "max_length": 20,
                    "choices": [{"name": "safe", "value": "safe"}],
                }
            ],
        },
        "command_id": "91",
        "options": None,
        "type": "command",
        "context": "guild",
        "integration_type": "guild_install",
        "installation_id": "50",
        "installation_revision": "1",
        "resolved": {"attachments": {}},
        "encrypted_payload": {
            "version": 2,
            "protocol": "mls10",
            "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
            "group_id": b64url(b"g" * 32),
            "policy_generation": "3",
            "epoch": "7",
            "sender_device_id": "ked_" + b64url(b"d" * 32),
            "operation": "create",
            "ciphertext": b64url(b"ciphertext"),
        },
    }


def prepared_interaction(
    *, interaction_id: int = 90
) -> tuple[Interaction, InteractionE2EEContext, FakeProvider]:
    bot = client()
    interaction = Interaction.from_payload(
        bot,
        "https://guild.example",
        interaction_payload(interaction_id=interaction_id),
    )
    provider = FakeProvider()
    context = InteractionE2EEContext(
        provider,
        interaction.channel_ref,
        b"g" * 32,
        3,
        7,
    )
    authenticated = interaction_authenticated_context(
        interaction, interaction.encrypted_payload or {}
    )
    provider.result = {
        "kind": "application",
        "application": b64url(
            interaction_plaintext(authenticated, options={"query": "safe"})
        ),
        "aad": b64url(interaction_authenticated_data(authenticated)),
        "credential": b64url(
            json.dumps(
                {
                    "version": 1,
                    "account": "40@users.example",
                    "nonce": b64url(b"n" * 32),
                },
                separators=(",", ":"),
            ).encode()
        ),
    }
    return interaction, context, provider


def test_decrypt_interaction_authenticates_context_and_options() -> None:
    interaction, context, _provider = prepared_interaction()

    decrypted = decrypt_interaction(interaction, context)

    assert interaction.command_id == 91
    assert decrypted.options == {"query": "safe"}
    assert decrypted.values == ()
    assert decrypted.components == ()


def test_encrypt_private_interaction_response_binds_exact_lifecycle_identity() -> None:
    interaction, context, _provider = prepared_interaction()
    interaction.client.set_e2ee_device("kbe_" + b64url(b"b" * 32))
    manifests = {"101@guild.example": encrypted_file_manifest()}

    encrypted = encrypt_interaction_response(
        interaction,
        context,
        {"content": "private"},
        callback_type=4,
        response_id=101,
        sequence=2,
        revision=3,
        attachment_manifests=manifests,
    )

    assert encrypted.response_id == 101
    assert encrypted.sequence == 2
    assert encrypted.revision == 3
    assert encrypted.envelope == {
        "version": 2,
        "protocol": "mls10",
        "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
        "group_id": b64url(b"g" * 32),
        "policy_generation": "3",
        "epoch": "7",
        "sender_device_id": "kbe_" + b64url(b"b" * 32),
        "operation": "edit",
        "interaction_ref": "90@guild.example",
        "response_ref": "101@guild.example",
        "sequence": "2",
        "revision": "3",
        "callback_type": 4,
        "attachment_refs": ["101@guild.example"],
        "target_message": "101@guild.example",
        "ciphertext": b64url(
            interaction_response_plaintext(
                encrypted.context,
                {
                    "content": "private",
                    "attachments": manifests,
                },
            )
        ),
    }
    assert json.loads(interaction_response_authenticated_data(encrypted.context)) == {
        "context": encrypted.context,
        "purpose": "kaede.interaction.response.v1",
    }

    with pytest.raises(ValueError, match="identity"):
        encrypt_interaction_response(
            interaction,
            context,
            {},
            callback_type=8,
            response_id=0,
        )


def test_python_matches_shared_routing_contract_vectors() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "frontend/static/protocol/interaction-routing-contract-v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    for vector in fixture["vectors"]:
        contract = interaction_routing_contract(
            vector["input"],
            callback_type=vector["callback_type"],
        )
        assert contract == vector["contract"], vector["name"]
        assert interaction_routing_contract_digest(contract) == vector["digest"]

    duplicate = fixture["vectors"][0]["input"]
    duplicate = json.loads(json.dumps(duplicate, ensure_ascii=False))
    options = duplicate["components"][0]["components"][0]["options"]
    options[1]["value"] = options[0]["value"]
    with pytest.raises(ValueError, match="unique"):
        interaction_routing_contract(duplicate, callback_type=4)


def test_python_matches_shared_response_aad_vectors() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "frontend/static/protocol/interaction-response-aad-v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    for vector in fixture["vectors"]:
        expected = vector["context"]
        interaction = Interaction.from_payload(
            client(),
            "https://guild.example",
            interaction_payload(
                interaction_id=int(expected["interaction_ref"].split("@", 1)[0])
            ),
        )
        envelope = {
            "interaction_ref": expected["interaction_ref"],
            "response_ref": expected["response_ref"],
            "sequence": expected["sequence"],
            "revision": expected["revision"],
            "callback_type": expected["callback_type"],
            "attachment_refs": expected["attachment_refs"],
            "group_id": expected["group_id"],
            "policy_generation": expected["policy_generation"],
            "epoch": expected["epoch"],
            "sender_device_id": expected["sender_device_id"],
            "operation": expected["operation"],
        }
        if expected["interaction_contract_digest"] is not None:
            envelope["interaction_contract_digest"] = expected[
                "interaction_contract_digest"
            ]
        actual = interaction_response_authenticated_context(interaction, envelope)

        assert actual == expected, vector["name"]
        assert (
            b64url(interaction_response_authenticated_data(actual))
            == vector["aad_base64url"]
        )
        contract = interaction_routing_contract(
            vector["data"],
            callback_type=expected["callback_type"],
        )
        assert contract == vector["interaction_contract"]


def test_encrypted_modal_response_carries_only_routing_commitments() -> None:
    interaction, context, _provider = prepared_interaction()
    interaction.client.set_e2ee_device("kbe_" + b64url(b"b" * 32))
    modal = {
        "title": "Private label",
        "custom_id": "settings",
        "components": [
            {
                "type": 18,
                "label": "Private choice label",
                "component": {
                    "type": 21,
                    "custom_id": "mode",
                    "options": [
                        {"label": "Private option", "value": "café"},
                        {"label": "Other option", "value": "βeta"},
                    ],
                    "required": True,
                },
            }
        ],
    }

    encrypted = encrypt_interaction_response(
        interaction,
        context,
        modal,
        callback_type=9,
        response_id=102,
    )

    contract = encrypted.envelope["interaction_contract"]
    serialized = json.dumps(contract, ensure_ascii=False)
    assert "Private" not in serialized
    assert "café" not in serialized
    assert "βeta" not in serialized
    assert (
        encrypted.context["interaction_contract_digest"]
        == encrypted.envelope["interaction_contract_digest"]
    )


def test_python_matches_shared_browser_aad_vectors() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "frontend/static/protocol/interaction-aad-v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    for vector in fixture["vectors"]:
        expected = vector["context"]
        payload = interaction_payload()
        payload.update(
            {
                "application_ref": expected["application_ref"],
                "channel_ref": expected["channel_ref"],
                "command_id": expected["command_id"],
                "command": (
                    {
                        "name": expected["command_name"],
                        "type": expected["command_type"],
                        "description": "",
                    }
                    if expected["command_name"] is not None
                    else None
                ),
                "type": expected["interaction_type"],
                "context": expected["context"],
                "integration_type": expected["integration_type"],
                "message_ref": expected["message_ref"],
                "response_id": expected["response_id"],
                "view_version": expected["view_version"],
                "autocomplete_generation": expected["autocomplete_generation"],
                "focused_option": expected["focused_option"],
                "target_ref": expected["target_ref"],
                "custom_id": expected["custom_id"],
                "component_type": expected["component_type"],
                "resolved": {"attachments": {}},
            }
        )
        payload["encrypted_payload"].update(
            {
                "group_id": expected["group_id"],
                "policy_generation": expected["policy_generation"],
                "epoch": expected["epoch"],
                "sender_device_id": expected["sender_device_id"],
            }
        )
        interaction = Interaction.from_payload(
            client(),
            "https://guild.example",
            payload,
        )

        actual = interaction_authenticated_context(
            interaction,
            interaction.encrypted_payload or {},
        )

        assert actual == expected, vector["name"]
        assert (
            b64url(interaction_authenticated_data(actual)) == vector["aad_base64url"]
        ), vector["name"]


def test_decrypt_interaction_rejects_aad_tamper_and_bad_options() -> None:
    interaction, context, provider = prepared_interaction()
    provider.result["aad"] = b64url(b"wrong context")
    with pytest.raises(E2EEProtocolError, match="authenticated context"):
        decrypt_interaction(interaction, context)

    authenticated = interaction_authenticated_context(
        interaction, interaction.encrypted_payload or {}
    )
    provider.result["aad"] = b64url(interaction_authenticated_data(authenticated))
    provider.result["application"] = b64url(
        interaction_plaintext(authenticated, options={"query": "not-a-choice"})
    )
    with pytest.raises(E2EEProtocolError, match="choices"):
        decrypt_interaction(interaction, context)


def test_decrypt_interaction_rejects_cross_interaction_replay() -> None:
    first, context, provider = prepared_interaction(interaction_id=90)
    decrypt_interaction(first, context)
    replay = Interaction.from_payload(
        first.client,
        first.target,
        interaction_payload(interaction_id=91),
    )
    replay_context = interaction_authenticated_context(
        replay, replay.encrypted_payload or {}
    )
    provider.result["application"] = b64url(
        interaction_plaintext(replay_context, options={"query": "safe"})
    )
    provider.result["aad"] = b64url(interaction_authenticated_data(replay_context))

    with pytest.raises(E2EEProtocolError, match="replayed"):
        decrypt_interaction(replay, context)


def encrypted_file_manifest() -> dict[str, object]:
    plaintext_size = 5
    chunk_size = 64 * 1024
    return {
        "version": 1,
        "protocol": "kaede-file-v1",
        "file_id": b64url(b"f" * 16),
        "key": b64url(b"k" * 32),
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "plaintext_size": plaintext_size,
        "ciphertext_size": plaintext_size + 41 + 20,
        "ciphertext_sha256": b64url(b"h" * 32),
        "chunk_size": chunk_size,
        "attachment_id": "101",
        "attachment_domain": "guild.example",
    }


def test_decrypt_interaction_authenticates_encrypted_file_manifest() -> None:
    payload = interaction_payload()
    payload["command"]["options"] = [
        {
            "name": "file",
            "description": "PDF",
            "type": "attachment",
            "required": True,
            "file_types": [".pdf"],
        }
    ]
    manifest = encrypted_file_manifest()
    attachments = {"101": manifest}
    payload["resolved"] = {
        "attachments": {
            "101": {
                "id": "101",
                "origin_domain": "guild.example",
                "filename": "encrypted-file",
                "content_type": "application/octet-stream",
                "size": manifest["ciphertext_size"],
                "encryption_mode": "e2ee",
                "encryption_protocol": "kaede-file-v1",
            }
        }
    }
    payload["encrypted_payload"]["attachment_manifest_digest"] = (
        interaction_attachment_manifest_digest(attachments)
    )
    interaction = Interaction.from_payload(
        client(),
        "https://guild.example",
        payload,
    )
    provider = FakeProvider()
    context = InteractionE2EEContext(
        provider,
        interaction.channel_ref,
        b"g" * 32,
        3,
        7,
    )
    authenticated = interaction_authenticated_context(
        interaction,
        interaction.encrypted_payload or {},
    )
    provider.result = {
        "kind": "application",
        "application": b64url(
            interaction_plaintext(
                authenticated,
                options={"file": "101"},
                attachments=attachments,
            )
        ),
        "aad": b64url(interaction_authenticated_data(authenticated)),
        "credential": b64url(
            json.dumps(
                {
                    "version": 1,
                    "account": "40@users.example",
                    "nonce": b64url(b"n" * 32),
                },
                separators=(",", ":"),
            ).encode()
        ),
    }

    decrypted = decrypt_interaction(interaction, context)

    assert decrypted.options == {"file": "101"}
    assert decrypted.attachments == attachments

    bad_manifest = {**manifest, "filename": "report.exe"}
    bad_attachments = {"101": bad_manifest}
    payload["encrypted_payload"]["attachment_manifest_digest"] = (
        interaction_attachment_manifest_digest(bad_attachments)
    )
    bad_interaction = Interaction.from_payload(
        interaction.client,
        interaction.target,
        payload,
    )
    bad_context = interaction_authenticated_context(
        bad_interaction,
        bad_interaction.encrypted_payload or {},
    )
    provider.result["application"] = b64url(
        interaction_plaintext(
            bad_context,
            options={"file": "101"},
            attachments=bad_attachments,
        )
    )
    provider.result["aad"] = b64url(interaction_authenticated_data(bad_context))
    with pytest.raises(E2EEProtocolError, match="file types"):
        decrypt_interaction(bad_interaction, context)


@pytest.mark.asyncio
async def test_client_decrypts_before_command_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction, context, provider = prepared_interaction()
    bot = interaction.client
    received: list[dict[str, Any] | None] = []

    async def handler(model: Interaction) -> None:
        received.append(model.options)

    bot._handlers["COMMAND:secure"].append(handler)  # noqa: SLF001
    bot.set_e2ee_device("kbe_" + b64url(b"b" * 32))
    bot.set_interaction_e2ee_context(context)
    monkeypatch.setattr(Client, "_sync_e2ee_control_log", AsyncMock())
    await bot.dispatch(
        "INTERACTION_CREATE",
        interaction_payload(),
        target="https://guild.example",
    )

    assert received == [{"query": "safe"}]
    assert provider.epoch == 7
