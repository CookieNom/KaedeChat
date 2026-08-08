from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api import channels as channel_api
from app.federation.schemas import GuildPinProxyRequest


def test_guild_pin_proxy_schema_rejects_unknown_fields() -> None:
    payload = {
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
        "pinned": True,
    }
    parsed = GuildPinProxyRequest.model_validate(payload)
    assert parsed.message_id.id == 75512661369970690
    with pytest.raises(ValueError):
        GuildPinProxyRequest.model_validate({**payload, "unexpected": True})


@pytest.mark.asyncio
async def test_remote_guild_pin_is_sent_to_authoritative_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed = AsyncMock(return_value=httpx.Response(200, json={"pinned": True}))
    monkeypatch.setattr(channel_api, "signed_request", signed)
    access = SimpleNamespace(
        guild=SimpleNamespace(id=44, origin_domain="guild.example"),
        channel=SimpleNamespace(id=55),
    )
    actor = SimpleNamespace(id=66, origin_domain="member.example")
    monkeypatch.setattr(
        channel_api,
        "profile_from_user",
        lambda _actor: {"id": "66", "origin_domain": "member.example"},
    )

    response = await channel_api.proxy_remote_guild_pin(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="member.example")),
        cast(Any, access),
        cast(Any, actor),
        cast(Any, SimpleNamespace(resolve=lambda _domain: (77, "author.example"))),
        pinned=True,
    )

    assert response.status_code == 204
    assert signed.await_args.args[3:] == ("guild.example", "/_kaede/v1/guilds/44/proxy-pin")
    assert signed.await_args.kwargs["payload"] == {
        "actor": {"id": "66", "origin_domain": "member.example"},
        "channel_id": "55",
        "message_id": "77@author.example",
        "pinned": True,
    }


@pytest.mark.asyncio
async def test_remote_guild_pin_rejects_an_inconsistent_home_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        channel_api,
        "signed_request",
        AsyncMock(return_value=httpx.Response(200, json={"pinned": False})),
    )
    monkeypatch.setattr(channel_api, "profile_from_user", lambda _actor: {})
    access = SimpleNamespace(
        guild=SimpleNamespace(id=44, origin_domain="guild.example"),
        channel=SimpleNamespace(id=55),
    )

    with pytest.raises(HTTPException) as raised:
        await channel_api.proxy_remote_guild_pin(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="member.example")),
            cast(Any, access),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(resolve=lambda _domain: (77, "author.example"))),
            pinned=True,
        )
    assert raised.value.detail == {"code": "FEDERATED_WRITE_RESPONSE_INVALID"}
