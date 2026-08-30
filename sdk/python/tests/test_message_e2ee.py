from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kaede_bot.e2ee import (
    build_disclosed_forward_snapshot,
    build_encrypted_forward_snapshot,
    encrypted_forward_snapshot_digest,
    message_forward_projection_digest,
    message_rich_authenticated_data,
    message_rich_payload_digest,
    message_sticker_routing_refs,
)
from kaede_bot.models import Attachment, Message
from kaede_bot.refs import EntityRef


FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "frontend"
    / "static"
    / "protocol"
    / "message-rich-aad-v1.json"
)


def _digest(value: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(value).digest()).decode().rstrip("=")


def test_message_rich_cross_language_vectors() -> None:
    fixture: dict[str, Any] = json.loads(FIXTURE.read_text())
    expected_fields = set(fixture["context_fields"])
    for vector in fixture["vectors"]:
        context = vector["context"]
        data = vector["rich_data"]
        assert set(context) == expected_fields
        assert message_rich_payload_digest(data) == context["rich_payload_digest"]
        assert (
            message_forward_projection_digest(
                data,
                context["message_mention_refs"],
            )
            == context["forward_projection_digest"]
        )
        assert _digest(message_rich_authenticated_data(context)) == vector["aad_sha256"]


def test_message_rich_fixture_binds_private_routing_identities() -> None:
    fixture: dict[str, Any] = json.loads(FIXTURE.read_text())
    human_create, human_edit, bot_create, _bot_edit = fixture["vectors"]
    assert human_create["context"]["message_custom_emoji_refs"] == [
        "<:wave:88@apps.example>"
    ]
    assert human_edit["context"]["referenced_message_ref"] == "777@example.test"
    assert human_create["context"]["message_mention_user_refs"] == ["43@remote.test"]
    assert human_create["context"]["message_mention_role_refs"] == ["55@example.test"]
    assert human_create["context"]["message_mention_everyone"] is True
    assert human_edit["context"]["message_replied_user_ref"] == "99@example.test"
    assert bot_create["context"]["message_sticker_refs"] == ["77@apps.example"]
    assert {
        "message_custom_emoji_refs",
        "message_mention_everyone",
        "message_mention_role_refs",
        "message_mention_user_refs",
        "message_replied_user_ref",
        "message_sticker_refs",
        "referenced_message_ref",
    } <= set(fixture["negative_mutations"])


def test_forward_projection_is_stable_across_attachment_reencryption() -> None:
    common = {
        "content": "snapshot",
        "embeds": [],
        "components": [],
        "mention_user_refs": [],
        "sticker_items": [],
        "message_snapshots": [],
        "message_type": 0,
        "flags": 0,
        "created_at": "2026-08-28T00:00:00+00:00",
        "edited_at": None,
    }
    manifest = {
        "version": 1,
        "protocol": "kaede-file-v1",
        "file_id": "AAECAwQFBgcICQoLDA0ODw",
        "key": "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8",
        "filename": "voice.ogg",
        "content_type": "audio/ogg",
        "plaintext_size": 12,
        "ciphertext_size": 73,
        "ciphertext_sha256": "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE",
        "plaintext_sha256": "qUiQTy8PR5uPgZdpSzAYSw0u0cHNKh7A-4XSmaGSpEc",
        "chunk_size": 262144,
        "attachment_id": "501",
        "attachment_domain": "destination.example",
        "duration_millis": 1200,
        "waveform": "AQIDBA==",
    }
    encrypted = common | {"attachments": [manifest]}
    disclosed = common | {
        "attachments": [
            {
                "id": "601",
                "origin_domain": "other.example",
                "filename": "voice.ogg",
                "content_type": "audio/ogg",
                "size": 12,
                "plaintext_sha256": manifest["plaintext_sha256"],
                "duration_secs": 1.2,
                "waveform": "AQIDBA==",
                "scan_status": "clean",
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
                "variants": {},
            }
        ]
    }

    digest = encrypted_forward_snapshot_digest(encrypted)
    assert encrypted_forward_snapshot_digest(disclosed) == digest
    disclosed["attachments"][0]["plaintext_sha256"] = (  # type: ignore[index]
        "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg"
    )
    assert encrypted_forward_snapshot_digest(disclosed) != digest


def test_forwarded_stickers_join_the_authenticated_routing_union() -> None:
    def sticker(sticker_id: int, domain: str) -> dict[str, object]:
        return {
            "id": str(sticker_id),
            "origin_domain": domain,
            "name": f"sticker{sticker_id}",
            "format_type": 1,
            "media_hash": "a" * 64,
        }

    data = {
        "sticker_items": [sticker(30, "outer.example")],
        "forward_snapshot": {
            "sticker_items": [sticker(20, "source.example")],
            "message_snapshots": [
                {
                    "sticker_items": [sticker(10, "nested.example")],
                    "message_snapshots": [],
                }
            ],
        },
    }

    assert message_sticker_routing_refs(data) == [
        "10@nested.example",
        "20@source.example",
        "30@outer.example",
    ]


def test_forwarding_a_forward_rebinds_nested_attachment_manifests() -> None:
    source_manifest = {
        "version": 1,
        "protocol": "kaede-file-v1",
        "file_id": "AAECAwQFBgcICQoLDA0ODw",
        "key": "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8",
        "filename": "source.bin",
        "content_type": "application/octet-stream",
        "plaintext_size": 4,
        "ciphertext_size": 65,
        "ciphertext_sha256": "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE",
        "plaintext_sha256": "qUiQTy8PR5uPgZdpSzAYSw0u0cHNKh7A-4XSmaGSpEc",
        "chunk_size": 262144,
        "attachment_id": "501",
        "attachment_domain": "source.example",
    }
    destination_manifest = source_manifest | {
        "file_id": "Dw4NDAsKCQgHBgUEAwIBAA",
        "key": "Pz49PDs6OTg3NjU0MzIxMC8uLSwrKikoJyYlJCMiISA",
        "ciphertext_sha256": "YmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmI",
        "attachment_id": "601",
        "attachment_domain": "destination.example",
    }
    nested = {
        "content": "original",
        "embeds": [],
        "components": [],
        "attachments": [source_manifest],
        "mention_user_refs": [],
        "sticker_items": [],
        "message_snapshots": [],
        "message_type": 0,
        "flags": 0,
        "created_at": "2026-08-28T00:00:00+00:00",
        "edited_at": None,
    }
    expected = {
        "content": None,
        "embeds": [],
        "components": [],
        "attachments": [destination_manifest],
        "mention_user_refs": [],
        "sticker_items": [],
        "message_snapshots": [nested | {"attachments": [destination_manifest]}],
        "message_type": 0,
        "flags": 0,
        "created_at": "2026-08-28T01:00:00+00:00",
        "edited_at": None,
    }
    attachment = Attachment(
        client=None,  # type: ignore[arg-type]
        target="https://source.example",
        ref=EntityRef(501, "source.example"),
        filename="encrypted-file",
        content_type="application/octet-stream",
        size=65,
        scan_status="encrypted",
        encryption_mode="e2ee",
        encryption_protocol="kaede-file-v1",
        encrypted_manifest=source_manifest,
    )
    message = Message(
        client=None,  # type: ignore[arg-type]
        target="https://source.example",
        ref=EntityRef(700, "source.example"),
        channel_ref=EntityRef(70, "source.example"),
        author=None,
        content=None,
        created_at=datetime(2026, 8, 28, 1, tzinfo=UTC),
        attachments=[attachment],
        forward_snapshot=nested,
        e2ee={
            "forward_projection_version": 2,
            "forward_projection_digest": encrypted_forward_snapshot_digest(expected),
        },
    )

    rebound = build_encrypted_forward_snapshot(
        message,
        attachment_manifests=[destination_manifest],
    )

    assert rebound == expected
    assert source_manifest["key"] not in json.dumps(rebound)
    assert source_manifest["attachment_id"] not in json.dumps(rebound)

    disclosed_attachment = Attachment(
        client=None,  # type: ignore[arg-type]
        target="https://plaintext.example",
        ref=EntityRef(701, "plaintext.example"),
        filename="source.bin",
        content_type="application/octet-stream",
        size=4,
        scan_status="clean",
    )
    disclosed = build_disclosed_forward_snapshot(message, [disclosed_attachment])
    disclosed_nested = disclosed["message_snapshots"]
    assert isinstance(disclosed_nested, list)
    disclosed_binding = disclosed_nested[0]["attachments"][0]
    assert disclosed_binding["id"] == "701"
    assert disclosed_binding["origin_domain"] == "plaintext.example"
    assert disclosed_binding["plaintext_sha256"] == source_manifest["plaintext_sha256"]
    assert "key" not in disclosed_binding
