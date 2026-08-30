import pytest
from pydantic import BaseModel

from app.chat.schemas import (
    ChannelCreate,
    ChannelPositionBatch,
    InviteCreate,
    OverwritePut,
    RoleCreate,
)
from app.core.types import EntityRef, EntityReference, Snowflake
from app.federation.schemas import PresenceFederationRequest
from app.media.schemas import StickerCrop, StickerTicketRequest, UploadTicketRequest


class SnowflakePayload(BaseModel):
    id: Snowflake


def test_snowflake_serializes_as_string() -> None:
    payload = SnowflakePayload(id="9223372036854775807")
    assert payload.model_dump_json() == '{"id":"9223372036854775807"}'


@pytest.mark.parametrize("value", ["", "-1", "1.0", True, 1 << 63])
def test_snowflake_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        SnowflakePayload(id=value)  # type: ignore[arg-type]


class EntityReferencePayload(BaseModel):
    id: EntityRef


def test_entity_reference_is_a_canonical_string() -> None:
    payload = EntityReferencePayload(id="42@chat.example.com")
    assert payload.id.resolve("local.example.com") == (42, "chat.example.com")
    assert payload.model_dump_json() == '{"id":"42@chat.example.com"}'
    assert EntityReferencePayload(id="42").id.resolve("local.example.com") == (
        42,
        "local.example.com",
    )


@pytest.mark.parametrize(
    "value",
    [
        "01",
        "42@Chat.example.com",
        "42@chat.example.com.",
        "42@chat.example.com@evil.example",
        "42@localhost",
        42,
    ],
)
def test_entity_reference_rejects_noncanonical_wire_values(value: object) -> None:
    with pytest.raises(ValueError):
        EntityReferencePayload(id=value)  # type: ignore[arg-type]


def test_internal_entity_reference_enforces_invariants() -> None:
    with pytest.raises(ValueError):
        EntityReference(-1, "chat.example.com")
    with pytest.raises(ValueError):
        EntityReference(1, "Chat.example.com")


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ChannelCreate, {"name": "general", "parent_id": 9}),
        (InviteCreate, {"channel_id": 9}),
        (RoleCreate, {"name": "member", "permissions": 8}),
        (
            OverwritePut,
            {"target_id": "9", "target_type": "role", "allow": 8, "deny": "0"},
        ),
    ],
)
def test_json_body_snowflakes_reject_integer_values(
    model: type[BaseModel], payload: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        model.model_validate(payload)


def test_json_body_snowflakes_accept_canonical_decimal_strings() -> None:
    channel = ChannelCreate(name="general", parent_id="9")
    invite = InviteCreate(channel_id="9@guild.example.com")
    role = RoleCreate(name="member", permissions="8")
    assert channel.parent_id == 9
    assert invite.channel_id is not None
    assert invite.channel_id.resolve("local.example.com") == (9, "guild.example.com")
    assert role.permissions == 8


def test_invite_creation_accepts_exact_live_target() -> None:
    invite = InviteCreate.model_validate(
        {
            "channel_id": "9@guild.example.com",
            "target_type": "stream",
            "target_user_id": "10@apps.example.com",
            "scheduled_event_id": "11@guild.example.com",
        }
    )
    assert invite.target_type == "stream"
    assert invite.scheduled_event_id is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"target_type": "stream"},
        {"target_type": "embedded_application", "target_user_id": "10@apps.example"},
        {"target_application_id": "10@apps.example"},
        {"target_user_id": "10@apps.example"},
        {"target_type": "scheduled_event", "scheduled_event_id": "11@guild.example"},
    ],
)
def test_invite_creation_rejects_mismatched_live_target(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        InviteCreate.model_validate(payload)


def test_voice_and_stage_channel_limits_are_type_aware() -> None:
    assert ChannelCreate(name="stage", type=13, bitrate=64_000, user_limit=10_000)
    assert ChannelCreate(name="voice", type=2, bitrate=384_000, user_limit=99)
    for payload in (
        {"name": "stage", "type": 13, "bitrate": 64_001},
        {"name": "stage", "type": 13, "user_limit": 10_001},
        {"name": "voice", "type": 2, "user_limit": 100},
    ):
        with pytest.raises(ValueError):
            ChannelCreate.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "general", "type": True},
        {"name": "general", "rate_limit_per_user": False},
        {"name": "voice", "type": 2, "bitrate": True},
        {"name": "voice", "type": 2, "user_limit": False},
        {"name": "voice", "type": 2, "video_quality_mode": True},
    ],
)
def test_request_models_reject_booleans_for_integer_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        ChannelCreate.model_validate(payload)


def test_shared_wire_model_rejects_ambiguous_federation_and_media_values() -> None:
    with pytest.raises(ValueError, match="observed_at must be an integer"):
        PresenceFederationRequest.model_validate(
            {
                "user_id": "7",
                "user_domain": "remote.example",
                "status": "online",
                "observed_at": True,
                "expires_at": 10,
            }
        )
    with pytest.raises(ValueError, match="size must be an integer"):
        UploadTicketRequest.model_validate(
            {"filename": "image.png", "content_type": "image/png", "size": False}
        )
    with pytest.raises(ValueError, match="remove_background must be a boolean"):
        StickerTicketRequest.model_validate(
            {
                "filename": "sticker.png",
                "content_type": "image/png",
                "size": 128,
                "remove_background": 1,
            }
        )
    with pytest.raises(ValueError, match="x must be a JSON number"):
        StickerCrop.model_validate({"x": "0.1", "y": 0, "width": 0.5, "height": 0.5})
    with pytest.raises(ValueError, match="duration_secs must be a JSON number"):
        UploadTicketRequest.model_validate(
            {
                "filename": "voice.ogg",
                "content_type": "audio/ogg",
                "size": 128,
                "duration_secs": True,
                "waveform": "AQ==",
            }
        )


def test_channel_position_batch_accepts_discord_partial_patch_shapes() -> None:
    batch = ChannelPositionBatch.model_validate(
        {
            "channels": [
                {"id": "10", "position": 3},
                {"id": "11", "position": 3, "parent_id": None},
                {"id": "12", "position": None, "parent_id": "13"},
                {"id": "13", "lock_permissions": None, "flags": None},
            ]
        }
    )
    assert [item.id for item in batch.channels] == [10, 11, 12, 13]
    assert "parent_id" not in batch.channels[0].model_fields_set
    assert "parent_id" in batch.channels[1].model_fields_set
    assert batch.model_dump(mode="json", exclude_unset=True)["channels"][2:] == [
        {"id": "12", "position": None, "parent_id": "13"},
        {"id": "13", "lock_permissions": None, "flags": None},
    ]

    aliased = ChannelPositionBatch.model_validate(
        {"channels": [{"id": "12", "sync_permissions": True}]}
    )
    assert aliased.channels[0].lock_permissions is True
    assert aliased.model_dump(mode="json", exclude_unset=True) == {
        "channels": [{"id": "12", "lock_permissions": True}]
    }
    with pytest.raises(ValueError, match="lock_permissions must be a boolean or null"):
        ChannelPositionBatch.model_validate({"channels": [{"id": "12", "sync_permissions": 1}]})

    with pytest.raises(ValueError):
        ChannelPositionBatch.model_validate(
            {
                "channels": [
                    {"id": "10", "position": 0},
                    {"id": "10", "position": 1},
                ]
            }
        )
