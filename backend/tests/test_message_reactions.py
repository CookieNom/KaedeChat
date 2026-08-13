from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api import channels as channel_api
from app.federation.schemas import GuildReactionProxyRequest


def reaction_request() -> dict[str, object]:
    return {
        "actor": {
            "id": "75512661369970688",
            "origin_domain": "member.example",
            "username": "member",
            "display_name": None,
            "avatar_hash": None,
            "banner_hash": None,
            "bio": None,
            "custom_status": None,
            "profile_version": 1,
        },
        "channel_id": "75512661369970689",
        "message_id": "75512661369970690@author.example",
        "emoji": "🔥",
        "remove": False,
    }


def test_guild_reaction_proxy_schema_is_strict_and_bounded() -> None:
    parsed = GuildReactionProxyRequest.model_validate(reaction_request())
    assert parsed.emoji == "🔥"
    assert str(parsed.message_id) == "75512661369970690@author.example"
    with pytest.raises(ValueError):
        GuildReactionProxyRequest.model_validate({**reaction_request(), "unexpected": True})
    with pytest.raises(ValueError):
        GuildReactionProxyRequest.model_validate({**reaction_request(), "emoji": "x" * 321})


@pytest.mark.asyncio
async def test_remote_guild_reaction_is_sent_to_authoritative_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed = AsyncMock(return_value=httpx.Response(200, json={"reacted": True}))
    monkeypatch.setattr(channel_api, "signed_request", signed)
    monkeypatch.setattr(
        channel_api,
        "profile_from_user",
        lambda _actor: {"id": "66", "origin_domain": "member.example"},
    )
    access = SimpleNamespace(
        guild=SimpleNamespace(id=44, origin_domain="guild.example"),
        channel=SimpleNamespace(id=55),
    )

    response = await channel_api.proxy_remote_guild_reaction(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="member.example")),
        cast(Any, access),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(resolve=lambda _domain: (77, "author.example"))),
        "🔥",
        remove=False,
    )

    assert response.status_code == 204
    assert signed.await_args.args[3:] == (
        "guild.example",
        "/_kaede/v1/guilds/44/proxy-reaction",
    )
    assert signed.await_args.kwargs["payload"] == {
        "actor": {"id": "66", "origin_domain": "member.example"},
        "channel_id": "55",
        "message_id": "77@author.example",
        "emoji": "🔥",
        "remove": False,
    }


@pytest.mark.asyncio
async def test_remote_guild_reaction_rejects_inconsistent_home_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        channel_api,
        "signed_request",
        AsyncMock(return_value=httpx.Response(200, json={"reacted": False})),
    )
    monkeypatch.setattr(channel_api, "profile_from_user", lambda _actor: {})
    access = SimpleNamespace(
        guild=SimpleNamespace(id=44, origin_domain="guild.example"),
        channel=SimpleNamespace(id=55),
    )

    with pytest.raises(HTTPException) as raised:
        await channel_api.proxy_remote_guild_reaction(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="member.example")),
            cast(Any, access),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(resolve=lambda _domain: (77, "author.example"))),
            "🔥",
            remove=False,
        )
    assert raised.value.detail == {"code": "FEDERATED_WRITE_RESPONSE_INVALID"}
