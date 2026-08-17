from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

import app.api.federation as federation_api
from app.api import channels as channel_api
from app.core.settings import Settings
from app.db.models import User
from app.federation.schemas import GuildPinProxyRequest
from app.federation.security import FederationPrincipal


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


@pytest.mark.asyncio
async def test_guild_authority_signs_remote_pin_with_local_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = Settings(
        domain="guild.example",
        environment="test",
        secret_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
        media_s3_access_key="GK00000000000000000000000000000000",
        media_s3_secret_key="0" * 64,
    )
    actor = User(
        id=66,
        origin_domain="member.example",
        username="member",
        is_local=False,
    )
    owner = User(
        id=11,
        origin_domain="guild.example",
        username="owner",
        is_local=True,
    )
    guild = SimpleNamespace(
        id=44,
        origin_domain="guild.example",
        owner_id=owner.id,
        owner_domain=owner.origin_domain,
    )
    channel = SimpleNamespace(
        id=55,
        origin_domain="guild.example",
        guild_id=guild.id,
        unavailable=False,
        type=0,
    )
    message = SimpleNamespace(
        id=77,
        origin_domain="author.example",
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        deleted_at=None,
    )
    payload = GuildPinProxyRequest.model_validate(
        {
            "actor": {
                "id": str(actor.id),
                "origin_domain": actor.origin_domain,
                "username": actor.username,
            },
            "channel_id": str(channel.id),
            "message_id": f"{message.id}@{message.origin_domain}",
            "pinned": True,
        }
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[channel, message, owner]),
        scalar=AsyncMock(return_value=message.id),
        commit=AsyncMock(),
    )
    queue = AsyncMock(return_value=1)

    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(federation_api, "require_permissions", AsyncMock())
    monkeypatch.setattr(federation_api, "queue_guild_mutation", queue)
    monkeypatch.setattr(federation_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(federation_api, "publish_dispatch", AsyncMock())

    result = await federation_api.federation_guild_pin_proxy(
        guild_id=cast(Any, guild.id),
        payload=payload,
        principal=FederationPrincipal(actor.origin_domain, "ed25519:test"),
        session=cast(Any, session),
        redis=cast(Any, SimpleNamespace()),
        settings=configured,
    )

    assert result == {"pinned": True}
    assert queue.await_args.args[3] is owner
    assert queue.await_args.args[5]["user"] == {
        "id": str(actor.id),
        "origin_domain": actor.origin_domain,
    }
    session.commit.assert_awaited_once()
