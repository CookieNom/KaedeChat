from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.api.channels import validate_merged_message_edit
from app.chat.e2ee import validate_e2ee_envelope

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "static"
    / "protocol"
    / "message-rich-aad-v1.json"
)


def rich_envelope(vector: dict[str, object]) -> dict[str, object]:
    context = copy.deepcopy(vector["context"])
    assert isinstance(context, dict)
    context.pop("channel_ref")
    target_message = context.pop("target_message")
    manifest_digest = context.pop("attachment_manifest_digest")
    contract_digest = context.pop("interaction_contract_digest")
    envelope: dict[str, object] = {
        "version": 2,
        "protocol": "mls10",
        "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
        "ciphertext": "b3BhcXVl",
        **context,
    }
    if target_message is not None:
        envelope["target_message"] = target_message
    if manifest_digest is not None:
        envelope["attachment_manifest_digest"] = manifest_digest
    if contract_digest is not None:
        envelope["interaction_contract"] = copy.deepcopy(vector["interaction_contract"])
        envelope["interaction_contract_digest"] = contract_digest
    return envelope


def test_rich_v2_fixture_envelopes_are_backend_canonical() -> None:
    fixture = json.loads(FIXTURE.read_text())
    for vector in fixture["vectors"]:
        envelope = rich_envelope(vector)
        assert validate_e2ee_envelope(envelope) == envelope


@pytest.mark.parametrize(
    "field",
    [
        "message_sticker_refs",
        "message_custom_emoji_refs",
        "message_mention_everyone",
        "message_mention_role_refs",
        "message_mention_user_refs",
        "message_replied_user_ref",
        "referenced_message_ref",
    ],
)
def test_rich_v2_private_routing_bindings_are_required(field: str) -> None:
    fixture = json.loads(FIXTURE.read_text())
    envelope = rich_envelope(fixture["vectors"][0])
    envelope.pop(field)
    with pytest.raises(ValueError, match="identity is incomplete"):
        validate_e2ee_envelope(envelope)


def test_rich_v2_custom_emoji_routing_is_canonical() -> None:
    fixture = json.loads(FIXTURE.read_text())
    envelope = rich_envelope(fixture["vectors"][0])
    envelope["message_custom_emoji_refs"] = ["<:wave:88@APPS.EXAMPLE>"]
    with pytest.raises(ValueError, match="custom emoji reference is invalid"):
        validate_e2ee_envelope(envelope)


def test_rich_v2_mention_routing_is_canonical() -> None:
    fixture = json.loads(FIXTURE.read_text())
    envelope = rich_envelope(fixture["vectors"][0])
    envelope["message_mention_role_refs"] = [
        "55@example.test",
        "55@example.test",
    ]
    with pytest.raises(ValueError, match="sorted and unique"):
        validate_e2ee_envelope(envelope)

    envelope = rich_envelope(fixture["vectors"][0])
    envelope["message_mention_everyone"] = "true"
    with pytest.raises(ValueError, match="broad mention intent"):
        validate_e2ee_envelope(envelope)


def test_rich_v2_edit_accepts_authenticated_sticker_routing_metadata() -> None:
    assert not validate_merged_message_edit(
        content=None,
        e2ee={"rich_payload_digest": "opaque"},
        embeds=[],
        components=[],
        attachment_count=0,
        sticker_items=[{"id": "77", "origin_domain": "stickers.example"}],
        forward_snapshot=None,
        current_flags=0,
        requested_flags=0,
    )
