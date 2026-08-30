from datetime import UTC, datetime

import pytest

from app.chat.message_references import (
    build_qualified_message_reference,
    normalize_qualified_message_reference,
    validate_channel_follow_message_fields,
    validate_message_reference_projection,
)
from app.chat.payloads import message_payload
from app.db.models import Message
from app.federation.replication import replicated_message_create_fingerprint


def test_system_reference_builder_matches_discord_shapes_with_federation_qualifiers() -> None:
    assert build_qualified_message_reference(
        message_type=6,
        message_ref=(9, "author.example"),
        channel_ref=(5, "guild.example"),
        guild_ref=(4, "guild.example"),
    ) == {
        "type": 0,
        "message_id": "9",
        "message_domain": "author.example",
        "channel_id": "5",
        "channel_domain": "guild.example",
        "guild_id": "4",
        "guild_domain": "guild.example",
    }
    assert build_qualified_message_reference(
        message_type=12,
        channel_ref=(7, "source.example"),
        guild_ref=(3, "source.example"),
    ) == {
        "type": 0,
        "channel_id": "7",
        "channel_domain": "source.example",
        "guild_id": "3",
        "guild_domain": "source.example",
    }


@pytest.mark.parametrize(
    "invalid",
    [
        {"type": 0, "channel_id": "5"},
        {"type": 0, "channel_id": "05", "channel_domain": "guild.example"},
        {"type": 0, "channel_id": "5", "channel_domain": "Guild.example"},
        {
            "type": 0,
            "message_id": None,
            "message_domain": None,
            "channel_id": "5",
            "channel_domain": "guild.example",
        },
        {
            "type": 0,
            "channel_id": "5",
            "channel_domain": "guild.example",
            "unexpected": True,
        },
    ],
)
def test_reference_normalization_rejects_ambiguous_or_noncanonical_wire(invalid: object) -> None:
    with pytest.raises(ValueError):
        normalize_qualified_message_reference(invalid)


def test_pin_projection_binds_message_channel_and_guild() -> None:
    reference = build_qualified_message_reference(
        message_type=6,
        message_ref=(9, "guild.example"),
        channel_ref=(5, "guild.example"),
        guild_ref=(4, "guild.example"),
    )
    assert (
        validate_message_reference_projection(
            reference,
            message_type=6,
            channel_ref=(5, "guild.example"),
            guild_ref=(4, "guild.example"),
            referenced_message_ref=(9, "guild.example"),
        )
        == reference
    )
    with pytest.raises(ValueError, match="does not match"):
        validate_message_reference_projection(
            reference | {"guild_id": "3"},
            message_type=6,
            channel_ref=(5, "guild.example"),
            guild_ref=(4, "guild.example"),
            referenced_message_ref=(9, "guild.example"),
        )


def test_follow_projection_retains_qualified_source_without_a_message_id() -> None:
    reference = build_qualified_message_reference(
        message_type=12,
        channel_ref=(7, "source.example"),
        guild_ref=(3, "source.example"),
    )
    assert (
        validate_message_reference_projection(
            reference,
            message_type=12,
            channel_ref=(5, "target.example"),
            guild_ref=(4, "target.example"),
            referenced_message_ref=None,
        )
        == reference
    )
    with pytest.raises(ValueError, match="cannot contain a message"):
        validate_message_reference_projection(
            reference | {"message_id": "8", "message_domain": "source.example"},
            message_type=12,
            channel_ref=(5, "target.example"),
            guild_ref=(4, "target.example"),
            referenced_message_ref=None,
        )


def test_channel_follow_system_fields_are_exact_and_immutable() -> None:
    raw: dict[str, object] = {"message_snapshots": []}
    rich: dict[str, object] = {
        "embeds": [],
        "components": [],
        "sticker_items": [],
        "poll": None,
        "application_ref": None,
        "interaction_metadata": None,
        "forwarded_ref": None,
        "forwarded_channel_ref": None,
        "forward_snapshot": None,
        "has_encrypted_forward": False,
    }
    fields: dict[str, object] = {
        "message_type": 12,
        "channel_type": 0,
        "content": "upstream-news",
        "e2ee": None,
        "attachments": [],
        "webhook": None,
        "mention_user_refs": [],
        "mention_role_refs": [],
        "mention_everyone": False,
        "flags": 0,
        "tts": False,
        "client_nonce": None,
        "referenced_message_ref": None,
    }
    validate_channel_follow_message_fields(raw, rich, **fields)  # type: ignore[arg-type]

    for key, value in (
        ("channel_type", 5),
        ("content", "x" * 101),
        ("content", " upstream-news"),
        ("e2ee", {"ciphertext": "forged"}),
        ("attachments", [{}]),
        ("webhook", {}),
        ("mention_user_refs", [(1, "user.example")]),
        ("mention_role_refs", [{"id": "1", "origin_domain": "target.example"}]),
        ("mention_everyone", True),
        ("flags", 1),
        ("tts", True),
        ("client_nonce", "forged"),
        ("referenced_message_ref", (1, "target.example")),
    ):
        with pytest.raises(ValueError):
            validate_channel_follow_message_fields(  # type: ignore[arg-type]
                raw,
                rich,
                **(fields | {key: value}),
            )

    for key, value in (
        ("embeds", [{"title": "forged"}]),
        ("components", [{"type": 1}]),
        ("sticker_items", [{"id": "1"}]),
        ("poll", {}),
        ("application_ref", (1, "apps.example")),
        ("interaction_metadata", {}),
        ("forwarded_ref", (1, "source.example")),
        ("forwarded_channel_ref", (2, "source.example")),
        ("forward_snapshot", {}),
        ("has_encrypted_forward", True),
    ):
        with pytest.raises(ValueError):
            validate_channel_follow_message_fields(  # type: ignore[arg-type]
                raw,
                rich | {key: value},
                **fields,
            )

    with pytest.raises(ValueError):
        validate_channel_follow_message_fields(  # type: ignore[arg-type]
            {"message_snapshots": [{"message": {}}]},
            rich,
            **fields,
        )


def test_message_payload_prefers_the_persisted_reference() -> None:
    reference = build_qualified_message_reference(
        message_type=6,
        message_ref=(9, "guild.example"),
        channel_ref=(5, "guild.example"),
        guild_ref=(4, "guild.example"),
    )
    message = Message(
        id=10,
        origin_domain="guild.example",
        channel_id=5,
        channel_domain="guild.example",
        author_id=2,
        author_domain="member.example",
        content=None,
        e2ee=None,
        message_type=6,
        flags=0,
        referenced_message_id=9,
        referenced_message_domain="guild.example",
        message_reference=reference,
        mention_user_refs=[],
        mention_role_refs=[],
        embeds=[],
        components=[],
        sticker_items=[],
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert message_payload(message)["message_reference"] == reference


def test_replay_fingerprint_binds_the_exact_reference_projection() -> None:
    shared = {
        "channel_id": 5,
        "channel_domain": "guild.example",
        "author_id": 2,
        "author_domain": "member.example",
        "content": None,
        "e2ee": None,
        "message_type": 6,
        "flags": 0,
        "client_nonce": None,
        "referenced_message_id": 9,
        "referenced_message_domain": "guild.example",
        "mention_user_refs": [],
        "mention_role_refs": [],
        "mention_everyone": False,
        "created_at": datetime(2026, 8, 29, tzinfo=UTC),
    }
    guild_reference = build_qualified_message_reference(
        message_type=6,
        message_ref=(9, "guild.example"),
        channel_ref=(5, "guild.example"),
        guild_ref=(4, "guild.example"),
    )
    wrong_reference = guild_reference | {"guild_id": "3"}

    assert replicated_message_create_fingerprint(
        **shared,
        message_reference=guild_reference,
    ) != replicated_message_create_fingerprint(
        **shared,
        message_reference=wrong_reference,
    )
