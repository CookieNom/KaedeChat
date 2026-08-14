from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api import channels as channel_api
from app.core.types import EntityRef
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


def reaction_user(identifier: int, domain: str, username: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier,
        origin_domain=domain,
        username=username,
        display_name=None,
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_version=1,
        profile_resolved=True,
    )


@pytest.mark.asyncio
async def test_reaction_users_are_permission_checked_and_composite_paginated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = reaction_user(9, "local.example", "viewer")
    access = SimpleNamespace(
        channel=SimpleNamespace(id=20, origin_domain="remote.example"),
        guild=SimpleNamespace(id=30, origin_domain="remote.example"),
    )
    message = SimpleNamespace(id=40, origin_domain="author.example")
    users = [
        reaction_user(7, "alpha.example", "maple"),
        reaction_user(7, "beta.example", "cedar"),
        reaction_user(8, "alpha.example", "birch"),
    ]
    session = AsyncMock()
    session.scalars.return_value = users
    session.scalar.return_value = 3
    load_access = AsyncMock(return_value=access)
    require_permissions = AsyncMock()
    load_message = AsyncMock(return_value=message)
    monkeypatch.setattr(channel_api, "load_channel_access", load_access)
    monkeypatch.setattr(channel_api, "require_channel_permissions", require_permissions)
    monkeypatch.setattr(channel_api, "channel_message", load_message)

    payload = await channel_api.list_reaction_users(
        channel_id=EntityRef("20@remote.example"),
        message_id=EntityRef("40@author.example"),
        emoji="🔥",
        after=None,
        limit=2,
        auth=cast(Any, SimpleNamespace(user=actor)),
        session=cast(Any, session),
        redis=cast(Any, SimpleNamespace()),
        settings=cast(Any, SimpleNamespace(domain="local.example")),
    )

    load_access.assert_awaited_once()
    require_permissions.assert_awaited_once()
    load_message.assert_awaited_once()
    assert payload["total"] == 3
    assert payload["next_after"] == "7@beta.example"
    assert [
        (item["id"], item["origin_domain"])
        for item in cast(list[dict[str, object]], payload["items"])
    ] == [
        ("7", "alpha.example"),
        ("7", "beta.example"),
    ]
