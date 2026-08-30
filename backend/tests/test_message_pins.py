from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

import app.api.federation as federation_api
from app.api import bots as bots_api
from app.api import channels as channel_api
from app.chat.channel_access import ChannelAccess
from app.chat.message_references import build_qualified_message_reference
from app.chat.pins import (
    CHANNEL_PIN_LIMIT,
    authority_attested_direct_pin_notice,
    message_is_pinnable,
    normalize_pin_cursor,
    validate_pin_page_payload,
)
from app.core.channel_types import is_pinnable_guild_channel_type
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import User
from app.federation.schemas import ChannelPinsPageProxyRequest, GuildPinProxyRequest
from app.federation.security import FederationPrincipal


@pytest.mark.parametrize("channel_type", [0, 5, 10, 11, 12, 15])
def test_discord_text_contexts_are_pinnable(channel_type: int) -> None:
    assert is_pinnable_guild_channel_type(channel_type)


@pytest.mark.parametrize("channel_type", [2, 13, 17])
def test_voice_and_stage_embedded_chat_are_not_pinnable(channel_type: int) -> None:
    assert not is_pinnable_guild_channel_type(channel_type)


def test_pin_contract_matches_current_discord_limits_and_message_types() -> None:
    assert CHANNEL_PIN_LIMIT == 250
    for message_type in (0, 19, 20, 23):
        assert message_is_pinnable(
            cast(Any, SimpleNamespace(message_type=message_type, deleted_at=None))
        )
    for message_type in (3, 6, 18, 46):
        assert not message_is_pinnable(
            cast(Any, SimpleNamespace(message_type=message_type, deleted_at=None))
        )
    with pytest.raises(ValueError, match="timezone"):
        normalize_pin_cursor(datetime(2026, 8, 29))


def test_federated_pin_page_is_bounded_linked_and_newest_first() -> None:
    payload: dict[str, object] = {
        "items": [
            {
                "pinned_at": "2026-08-29T02:00:00+00:00",
                "message": {
                    "id": "9",
                    "origin_domain": "author.example",
                    "channel_id": "5",
                    "channel_domain": "guild.example",
                    "pinned": True,
                },
            }
        ],
        "has_more": False,
    }
    assert (
        validate_pin_page_payload(
            payload,
            channel_ref=(5, "guild.example"),
            limit=50,
            before=datetime(2026, 8, 29, 3, tzinfo=UTC),
        )
        is payload
    )
    broken = {
        **payload,
        "items": [
            {
                **cast(dict[str, object], cast(list[object], payload["items"])[0]),
                "message": {
                    "id": "9",
                    "origin_domain": "author.example",
                    "channel_id": "6",
                    "channel_domain": "guild.example",
                    "pinned": True,
                },
            }
        ],
    }
    with pytest.raises(ValueError, match="linkage"):
        validate_pin_page_payload(
            broken,
            channel_ref=(5, "guild.example"),
            limit=50,
            before=None,
        )


def test_direct_pin_notice_authority_attestation_is_narrow() -> None:
    content: dict[str, object] = {
        "message": {
            "id": "11",
            "origin_domain": "dm.example",
            "channel_id": "5",
            "channel_domain": "dm.example",
            "author_id": "7",
            "author_domain": "member.example",
            "message_type": 6,
            "content": None,
            "e2ee": None,
            "attachments": [],
            "flags": 0,
            "referenced_message_id": "9",
            "referenced_message_domain": "member.example",
            "message_reference": build_qualified_message_reference(
                message_type=6,
                message_ref=(9, "member.example"),
                channel_ref=(5, "dm.example"),
            ),
        },
        "author": {"id": "7", "origin_domain": "member.example"},
    }
    assert authority_attested_direct_pin_notice(
        "dm.message.create",
        content,
        expected_authority="dm.example",
        actor=("7", "member.example"),
    )
    cast(dict[str, object], content["message"])["content"] = "smuggled"
    assert not authority_attested_direct_pin_notice(
        "dm.message.create",
        content,
        expected_authority="dm.example",
        actor=("7", "member.example"),
    )


@pytest.mark.asyncio
async def test_human_pin_rejects_voice_before_permission_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = ChannelAccess(
        channel=cast(Any, SimpleNamespace(type=2)),
        guild=cast(Any, SimpleNamespace()),
        participants=[],
    )
    monkeypatch.setattr(
        channel_api,
        "load_channel_access",
        AsyncMock(return_value=access),
    )
    permissions = AsyncMock()
    monkeypatch.setattr(channel_api, "require_channel_permissions", permissions)

    with pytest.raises(HTTPException) as raised:
        await channel_api.pin_message(
            EntityRef("55@guild.example"),
            EntityRef("77@guild.example"),
            cast(Any, SimpleNamespace(user=SimpleNamespace())),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )
    assert raised.value.detail == {"code": "PINS_UNSUPPORTED_CHANNEL"}
    permissions.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_pins_returns_empty_without_read_message_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = ChannelAccess(
        channel=cast(
            Any,
            SimpleNamespace(id=55, origin_domain="guild.example", type=0),
        ),
        guild=cast(
            Any,
            SimpleNamespace(id=44, origin_domain="guild.example"),
        ),
        participants=[],
    )
    monkeypatch.setattr(
        channel_api,
        "load_channel_access",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(
        channel_api,
        "require_channel_permissions",
        AsyncMock(return_value=int(Permission.VIEW_CHANNEL)),
    )
    session = SimpleNamespace(execute=AsyncMock())

    page = await channel_api.list_channel_pins(
        EntityRef("55@guild.example"),
        None,
        50,
        cast(Any, SimpleNamespace(user=SimpleNamespace())),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert page == {"items": [], "has_more": False}
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("pinned", "action_type"), [(True, 74), (False, 75)])
async def test_guild_pin_audit_actions_match_discord(
    monkeypatch: pytest.MonkeyPatch,
    pinned: bool,
    action_type: int,
) -> None:
    add_entry = AsyncMock()
    monkeypatch.setattr(channel_api, "add_audit_entry", add_entry)
    guild = SimpleNamespace(id=44, origin_domain="guild.example")
    channel = SimpleNamespace(id=55, origin_domain="guild.example")
    actor = SimpleNamespace(id=66, origin_domain="member.example")
    message = SimpleNamespace(id=77, origin_domain="guild.example")

    await channel_api.record_pin_audit_entry(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, ChannelAccess(channel=channel, guild=guild, participants=[])),
        cast(Any, actor),
        cast(Any, message),
        pinned=pinned,
        reason="  retained context  ",
    )

    assert add_entry.await_args.args[4] == action_type
    assert add_entry.await_args.kwargs["target_ref"] == {
        "id": "77",
        "origin_domain": "guild.example",
        "channel_id": "55",
        "channel_domain": "guild.example",
    }
    assert add_entry.await_args.kwargs["reason"] == "retained context"


@pytest.mark.asyncio
async def test_bot_pin_reuses_text_only_channel_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = SimpleNamespace()
    monkeypatch.setattr(
        bots_api,
        "installation_for_channel",
        AsyncMock(return_value=(SimpleNamespace(type=2), SimpleNamespace())),
    )
    monkeypatch.setattr(
        bots_api,
        "user_auth",
        lambda _principal: SimpleNamespace(user=SimpleNamespace()),
    )
    monkeypatch.setattr(
        channel_api,
        "load_channel_access",
        AsyncMock(
            return_value=ChannelAccess(
                channel=cast(Any, SimpleNamespace(type=2)),
                guild=cast(Any, SimpleNamespace()),
                participants=[],
            )
        ),
    )

    with pytest.raises(HTTPException) as raised:
        await bots_api.bot_pin_message(
            EntityRef("55@guild.example"),
            EntityRef("77@guild.example"),
            cast(Any, principal),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )
    assert raised.value.detail == {"code": "PINS_UNSUPPORTED_CHANNEL"}


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


def test_federated_pin_page_schema_normalizes_aware_cursor() -> None:
    payload = ChannelPinsPageProxyRequest.model_validate(
        {
            "actor": {
                "id": "75512661369970688",
                "origin_domain": "member.example",
                "username": "member",
            },
            "channel_id": "75512661369970689",
            "before": "2026-08-29T03:00:00+01:00",
            "limit": 25,
        }
    )
    assert payload.before == datetime(2026, 8, 29, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        ChannelPinsPageProxyRequest.model_validate(
            {
                "actor": payload.actor.model_dump(mode="json"),
                "channel_id": str(payload.channel_id),
                "before": "2026-08-29T03:00:00",
            }
        )


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
        "reason": None,
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
        message_type=0,
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
        get=AsyncMock(side_effect=[channel, message, None, owner]),
        scalar=AsyncMock(return_value=message.id),
        commit=AsyncMock(),
    )
    queue = AsyncMock(return_value=1)
    notice = {"id": "88", "message_type": 6}

    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    rate_limit = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", rate_limit)
    monkeypatch.setattr(
        federation_api,
        "require_remote_user_creation_allowed",
        AsyncMock(),
    )
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(federation_api, "require_permissions", AsyncMock())
    monkeypatch.setattr(federation_api, "channel_pin_count", AsyncMock(return_value=0))
    persist_notice = AsyncMock(return_value=(SimpleNamespace(), notice, set()))
    monkeypatch.setattr(federation_api, "persist_pin_notice", persist_notice)
    pin_audit = AsyncMock()
    monkeypatch.setattr(federation_api, "record_pin_audit_entry", pin_audit)
    monkeypatch.setattr(
        federation_api,
        "channel_pins_update_payload",
        AsyncMock(return_value={"channel_id": str(channel.id)}),
    )
    monkeypatch.setattr(federation_api, "queue_guild_mutation", queue)
    monkeypatch.setattr(federation_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(federation_api, "publish_dispatch", AsyncMock())

    result = await federation_api.federation_guild_pin_proxy(
        guild_id=cast(Any, guild.id),
        payload=payload,
        principal=FederationPrincipal(actor.origin_domain, "ed25519:test"),
        session=cast(Any, session),
        redis=cast(Any, SimpleNamespace()),
        snowflake=cast(Any, SimpleNamespace()),
        settings=configured,
    )

    assert result == {"pinned": True}
    assert rate_limit.await_args.args[1:] == (actor.origin_domain, "guild-pin-mutation")
    assert rate_limit.await_args.kwargs == {"capacity": 600, "refill_per_minute": 600}
    assert queue.await_args.args[3] is owner
    assert queue.await_args.args[5]["user"] == {
        "id": str(actor.id),
        "origin_domain": actor.origin_domain,
    }
    persist_notice.assert_awaited_once()
    assert pin_audit.await_args.kwargs["pinned"] is True
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_federated_pin_rejects_voice_channel_before_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=66,
        origin_domain="member.example",
        username="member",
        is_local=False,
    )
    guild = SimpleNamespace(id=44, origin_domain="guild.example")
    channel = SimpleNamespace(
        id=55,
        origin_domain="guild.example",
        guild_id=guild.id,
        unavailable=False,
        type=2,
    )
    payload = GuildPinProxyRequest.model_validate(
        {
            "actor": {
                "id": str(actor.id),
                "origin_domain": actor.origin_domain,
                "username": actor.username,
            },
            "channel_id": str(channel.id),
            "message_id": "77@author.example",
            "pinned": True,
        }
    )
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(
        federation_api,
        "require_remote_user_creation_allowed",
        AsyncMock(),
    )
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    permissions = AsyncMock()
    monkeypatch.setattr(federation_api, "require_permissions", permissions)

    with pytest.raises(HTTPException) as raised:
        await federation_api.federation_guild_pin_proxy(
            guild_id=cast(Any, guild.id),
            payload=payload,
            principal=FederationPrincipal(actor.origin_domain, "ed25519:test"),
            session=cast(Any, SimpleNamespace(get=AsyncMock(return_value=channel))),
            redis=cast(Any, SimpleNamespace()),
            settings=cast(Any, SimpleNamespace(domain="guild.example")),
        )
    assert raised.value.detail == {"code": "PINS_UNSUPPORTED_CHANNEL"}
    permissions.assert_not_awaited()
