from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from app.chat.custom_emojis import custom_emoji_refs, validate_custom_emoji_use
from app.core.permissions import Permission


def test_custom_emoji_refs_parse_and_deduplicate_federated_tokens() -> None:
    refs = custom_emoji_refs(
        "hello <:party:75512661369970688@alpha.example> "
        "<a:dance:75512661369970689@beta.example> "
        "<:party:75512661369970688@alpha.example>"
    )

    assert [(ref.id, ref.origin_domain, ref.name, ref.animated) for ref in refs] == [
        (75512661369970688, "alpha.example", "party", False),
        (75512661369970689, "beta.example", "dance", True),
    ]
    assert refs[1].token == "<a:dance:75512661369970689@beta.example>"


def test_custom_emoji_refs_ignore_noncanonical_tokens() -> None:
    assert (
        custom_emoji_refs(
            "<:x:0@bad> <:valid:123@example.invalid/path> "
            "<:huge:9999999999999999999@example.invalid>"
        )
        == ()
    )


class EmojiSession:
    def __init__(self, emoji: object | None, membership: int | None = 1) -> None:
        self.emoji = emoji
        self.membership = membership

    async def get(self, _model: object, _identity: object) -> object | None:
        return self.emoji

    async def scalar(self, _query: object) -> int | None:
        return self.membership


@pytest.mark.asyncio
async def test_external_custom_emoji_requires_destination_permission() -> None:
    emoji = SimpleNamespace(
        id=123,
        origin_domain="alpha.example",
        guild_id=10,
        guild_domain="alpha.example",
        name="party",
        animated=False,
    )
    actor = SimpleNamespace(id=20, origin_domain="alpha.example")
    destination = SimpleNamespace(id=30, origin_domain="beta.example")

    with pytest.raises(HTTPException) as raised:
        await validate_custom_emoji_use(
            cast(Any, EmojiSession(emoji)),
            cast(Any, actor),
            "<:party:123@alpha.example>",
            target_guild=cast(Any, destination),
            target_permissions=Permission.SEND_MESSAGES,
        )
    assert raised.value.detail == {"code": "USE_EXTERNAL_EMOJIS_REQUIRED"}


@pytest.mark.asyncio
async def test_external_custom_emoji_requires_source_membership() -> None:
    emoji = SimpleNamespace(
        id=123,
        origin_domain="alpha.example",
        guild_id=10,
        guild_domain="alpha.example",
        name="party",
        animated=False,
    )
    actor = SimpleNamespace(id=20, origin_domain="alpha.example")
    destination = SimpleNamespace(id=30, origin_domain="beta.example")

    with pytest.raises(HTTPException) as raised:
        await validate_custom_emoji_use(
            cast(Any, EmojiSession(emoji, membership=None)),
            cast(Any, actor),
            "<:party:123@alpha.example>",
            target_guild=cast(Any, destination),
            target_permissions=Permission.SEND_MESSAGES | Permission.USE_EXTERNAL_EMOJIS,
        )
    assert raised.value.detail == {"code": "CUSTOM_EMOJI_SOURCE_ACCESS_REQUIRED"}
