from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.client import Client, _expression_projection
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def test_expression_projection_covers_plain_rich_encrypted_and_forward_notes() -> None:
    assert _expression_projection(
        {
            "content": "<:text:1@s.example>",
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "emoji": {
                                "id": "2@s.example",
                                "name": "button",
                                "animated": True,
                            },
                        }
                    ],
                }
            ],
            "poll": {
                "answers": [
                    {"poll_media": {"emoji": {"id": "3@s.example", "name": "poll"}}}
                ]
            },
            "sticker_ids": ["4@s.example"],
        },
        default_domain="t.example",
    ) == {
        "s.example": (
            [
                "<:poll:3@s.example>",
                "<:text:1@s.example>",
                "<a:button:2@s.example>",
            ],
            ["4@s.example"],
        )
    }
    assert _expression_projection(
        {
            "e2ee": {
                "rich_payload_digest": "0" * 64,
                "message_custom_emoji_refs": ["<:secret:5@s.example>"],
                "message_sticker_refs": ["6@s.example"],
            }
        },
        default_domain="t.example",
    ) == {"s.example": (["<:secret:5@s.example>"], ["6@s.example"])}
    assert _expression_projection(
        {
            "forwarded_message_id": "7@f.example",
            "content": "note <:note:8@s.example>",
        },
        default_domain="t.example",
    ) == {"s.example": (["<:note:8@s.example>"], [])}
    assert (
        _expression_projection(
            {
                "forwarded_message_id": "7@f.example",
                "e2ee": {
                    "rich_payload_digest": "0" * 64,
                    "message_custom_emoji_refs": ["<:snapshot:9@s.example>"],
                },
            },
            default_domain="t.example",
        )
        == {}
    )


@pytest.mark.asyncio
async def test_bot_expression_intent_has_source_audience_and_target_runtime() -> None:
    bot = client()
    bot.fetch_channel = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(guild_ref=EntityRef(10, "t.example"))
    )
    sign = AsyncMock(return_value={"signed": True})
    bot._federated_actor_intent = sign  # type: ignore[method-assign]

    intents = await bot._message_expression_actor_intents(
        EntityRef(20, "t.example"),
        "https://t.example",
        {"content": "<:wave:88@s.example>"},
        operation="message.create",
        operation_id="create-1",
        target_message_ref=None,
        installation_id=30,
    )

    assert intents == {"s.example": {"signed": True}}
    call = sign.await_args
    assert call is not None
    assert call.kwargs["action"] == "expression.use.authorize"
    assert call.kwargs["audience"] == "https://s.example"
    assert call.kwargs["runtime_target"] == "https://t.example"
    resources = call.kwargs["resources"]
    assert resources["source_authority"] == "s.example"
    assert resources["target_guild_ref"] == "10@t.example"
    assert resources["target_channel_ref"] == "20@t.example"
    assert resources["operation"] == "message.create"
    assert resources["operation_id"] == "create-1"
