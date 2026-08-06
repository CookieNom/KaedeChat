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
    role = RoleCreate(name="member", permissions="8")
    assert channel.parent_id == 9
    assert role.permissions == 8


def test_channel_position_batch_requires_a_complete_unique_sequence() -> None:
    batch = ChannelPositionBatch.model_validate(
        {
            "channels": [
                {"id": "10", "position": 0, "parent_id": None},
                {"id": "11", "position": 1, "parent_id": "10"},
            ]
        }
    )
    assert [item.id for item in batch.channels] == [10, 11]

    for channels in (
        [
            {"id": "10", "position": 0},
            {"id": "10", "position": 1},
        ],
        [
            {"id": "10", "position": 0},
            {"id": "11", "position": 2},
        ],
    ):
        with pytest.raises(ValueError):
            ChannelPositionBatch.model_validate({"channels": channels})
