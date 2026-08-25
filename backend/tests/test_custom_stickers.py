from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from app.chat.custom_stickers import custom_sticker_refs, validate_custom_sticker_use
from app.core.permissions import Permission
from app.media.schemas import StickerTicketRequest


def test_custom_sticker_refs_parse_and_deduplicate() -> None:
    refs = custom_sticker_refs(
        "<sticker:wave:75512661369970688@alpha.example> "
        "<sticker:wave:75512661369970688@alpha.example>"
    )
    assert [(item.id, item.origin_domain, item.name) for item in refs] == [
        (75512661369970688, "alpha.example", "wave")
    ]
    assert refs[0].token == "<sticker:wave:75512661369970688@alpha.example>"


def test_sticker_crop_must_stay_inside_image() -> None:
    with pytest.raises(ValueError):
        StickerTicketRequest(
            filename="wave.png",
            content_type="image/png",
            size=10,
            crop={"x": 0.5, "y": 0, "width": 0.75, "height": 1},
        )


class StickerSession:
    def __init__(self, sticker: object | None, membership: int | None = 1) -> None:
        self.sticker = sticker
        self.membership = membership

    async def get(self, _model: object, _identity: object) -> object | None:
        return self.sticker

    async def scalar(self, _query: object) -> int | None:
        return self.membership


@pytest.mark.asyncio
async def test_external_sticker_requires_destination_permission() -> None:
    sticker = SimpleNamespace(
        id=123,
        origin_domain="alpha.example",
        guild_id=10,
        guild_domain="alpha.example",
        name="wave",
    )
    actor = SimpleNamespace(id=20, origin_domain="alpha.example")
    destination = SimpleNamespace(id=30, origin_domain="beta.example")
    with pytest.raises(HTTPException) as raised:
        await validate_custom_sticker_use(
            cast(Any, StickerSession(sticker)),
            cast(Any, actor),
            "<sticker:wave:123@alpha.example>",
            target_guild=cast(Any, destination),
            target_permissions=Permission.SEND_MESSAGES,
        )
    assert raised.value.detail == {"code": "USE_EXTERNAL_EMOJIS_REQUIRED"}
