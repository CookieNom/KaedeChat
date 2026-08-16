from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api import channels as channel_api
from app.api import federation as federation_api
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.federation.schemas import GuildReactionProxyRequest
from app.federation.security import FederationPrincipal


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
async def test_guild_home_signs_remote_actor_reaction_with_local_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = GuildReactionProxyRequest.model_validate(reaction_request())
    actor = SimpleNamespace(id=10, origin_domain="member.example", is_local=False)
    owner = SimpleNamespace(id=11, origin_domain="guild.example", is_local=True)
    guild = SimpleNamespace(
        id=12,
        origin_domain="guild.example",
        owner_id=owner.id,
        owner_domain=owner.origin_domain,
    )
    channel = SimpleNamespace(
        id=int(payload.channel_id),
        origin_domain="guild.example",
        guild_id=guild.id,
        unavailable=False,
        type=0,
    )
    message_id, message_domain = payload.message_id.resolve("guild.example")
    message = SimpleNamespace(
        id=message_id,
        origin_domain=message_domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        deleted_at=None,
    )
    session = AsyncMock()

    async def get(model: object, key: object) -> object | None:
        name = getattr(model, "__name__", "")
        if name == "Channel":
            return channel
        if name == "Message":
            return message
        if name == "User":
            return owner
        return None

    session.get.side_effect = get
    session.scalar.return_value = message.id
    queue_mutation = AsyncMock()
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        federation_api,
        "require_permissions",
        AsyncMock(return_value=Permission.ADD_REACTIONS),
    )
    monkeypatch.setattr(federation_api, "validate_custom_emoji_use", AsyncMock())
    monkeypatch.setattr(federation_api, "queue_guild_mutation", queue_mutation)
    monkeypatch.setattr(federation_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(federation_api, "publish_dispatch", AsyncMock())

    result = await federation_api.federation_guild_reaction_proxy(
        guild_id=guild.id,
        payload=payload,
        principal=FederationPrincipal("member.example", "ed25519:test"),
        session=cast(Any, session),
        redis=cast(Any, SimpleNamespace()),
        settings=cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result == {"reacted": True}
    assert queue_mutation.await_args.args[3] is owner
    assert queue_mutation.await_args.args[5]["user"] == {
        "id": str(actor.id),
        "origin_domain": actor.origin_domain,
    }
    session.commit.assert_awaited_once()


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
        account_type="user",
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
