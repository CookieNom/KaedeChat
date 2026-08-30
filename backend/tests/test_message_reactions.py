from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api import channels as channel_api
from app.api import federation as federation_api
from app.chat.reaction_payloads import (
    reaction_emoji_payload,
    reaction_event_payload,
    reaction_payloads_for_messages,
)
from app.chat.schemas import MessageBulkDelete
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.federation.guilds import apply_guild_mutation_event
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


def guild_reaction_event(emoji: str) -> dict[str, object]:
    return {
        "type": "guild.reaction.add",
        "context": {
            "guild_id": "12",
            "guild_domain": "guild.example",
            "channel_id": "30",
            "channel_domain": "guild.example",
            "seq": "1",
        },
        "actor": {"id": "10", "domain": "guild.example"},
        "content": {
            "message": {"id": "20", "origin_domain": "guild.example"},
            "user": {"id": "10", "origin_domain": "guild.example"},
            "emoji": emoji,
        },
    }


def guild_reaction_replica() -> tuple[SimpleNamespace, SimpleNamespace]:
    guild = SimpleNamespace(
        id=12,
        origin_domain="guild.example",
        last_event_seq=0,
        next_event_seq=1,
        sync_status="stale",
        permission_generation=1,
        snapshot_generation=1,
    )
    actor = SimpleNamespace(id=10, origin_domain="guild.example")
    message = SimpleNamespace(
        id=20,
        origin_domain="guild.example",
        author_id=10,
        author_domain="guild.example",
        channel_id=30,
        channel_domain="guild.example",
        deleted_at=None,
    )
    channel = SimpleNamespace(
        id=30,
        origin_domain="guild.example",
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=guild),
        get=AsyncMock(side_effect=[actor, message, actor, channel]),
        execute=AsyncMock(),
    )
    return guild, session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "emoji",
    [
        "lantern",
        "🏮🔥",
        "\ufe0f",
    ],
)
async def test_guild_reaction_event_rejects_invalid_emoji(emoji: str) -> None:
    guild, session = guild_reaction_replica()

    with pytest.raises(ValueError, match="reaction emoji is invalid"):
        await apply_guild_mutation_event(
            cast(Any, session),
            cast(Any, SimpleNamespace(domain="replica.example")),
            cast(Any, guild),
            guild_reaction_event(emoji),
        )

    session.execute.assert_not_awaited()
    assert guild.last_event_seq == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["guild.reaction.add", "guild.reaction.remove"])
@pytest.mark.parametrize(
    ("emoji", "canonical"),
    [
        ("🏮", "🏮"),
        ("❤️", "❤"),
        (
            "<:lantern:75512661369970689@GUILD.EXAMPLE.>",
            "<:lantern:75512661369970689@guild.example>",
        ),
    ],
)
async def test_guild_reaction_event_persists_canonical_emoji(
    event_type: str,
    emoji: str,
    canonical: str,
) -> None:
    guild, session = guild_reaction_replica()
    event = guild_reaction_event(emoji)
    event["type"] = event_type

    dispatch = await apply_guild_mutation_event(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="replica.example")),
        cast(Any, guild),
        event,
    )

    statement = session.execute.await_args.args[0]
    assert canonical in statement.compile().params.values()
    expected_dispatch = (
        "MESSAGE_REACTION_REMOVE" if event_type.endswith("remove") else "MESSAGE_REACTION_ADD"
    )
    assert dispatch is not None and dispatch[0] == expected_dispatch
    assert dispatch[1]["reaction"] == canonical
    assert dispatch[1]["message_id"] == "20"
    assert dispatch[1]["channel_id"] == "30"
    assert dispatch[1]["emoji"] == reaction_emoji_payload(canonical)
    assert dispatch[1]["removed"] is event_type.endswith("remove")
    assert guild.last_event_seq == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_emoji", "canonical", "dispatch_type"),
    [
        (None, None, "MESSAGE_REACTION_REMOVE_ALL"),
        ("❤️", "❤", "MESSAGE_REACTION_REMOVE_EMOJI"),
    ],
)
async def test_guild_reaction_clear_projects_the_authority_aggregate_event(
    raw_emoji: str | None,
    canonical: str | None,
    dispatch_type: str,
) -> None:
    guild = SimpleNamespace(
        id=12,
        origin_domain="guild.example",
        last_event_seq=0,
        next_event_seq=1,
        sync_status="stale",
        permission_generation=1,
        snapshot_generation=1,
    )
    actor = SimpleNamespace(id=10, origin_domain="guild.example")
    message = SimpleNamespace(
        id=20,
        origin_domain="guild.example",
        channel_id=30,
        channel_domain="guild.example",
    )
    channel = SimpleNamespace(
        id=30,
        origin_domain="guild.example",
        guild_id=12,
        guild_domain="guild.example",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=guild),
        get=AsyncMock(side_effect=[actor, message, channel]),
        execute=AsyncMock(),
    )
    content: dict[str, object] = {
        "message": {"id": "20", "origin_domain": "guild.example"},
    }
    if raw_emoji is not None:
        content["emoji"] = raw_emoji
    event = {
        "type": "guild.reaction.clear",
        "context": {
            "guild_id": "12",
            "guild_domain": "guild.example",
            "channel_id": "30",
            "channel_domain": "guild.example",
            "seq": "1",
        },
        "actor": {"id": "10", "domain": "guild.example"},
        "content": content,
    }

    dispatch = await apply_guild_mutation_event(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="replica.example")),
        cast(Any, guild),
        event,
    )

    expected_payload: dict[str, object] = {
        "message_id": "20",
        "message_domain": "guild.example",
        "channel_id": "30",
        "channel_domain": "guild.example",
        "guild_id": "12",
        "guild_domain": "guild.example",
    }
    if canonical is not None:
        expected_payload.update(
            {
                "reaction": canonical,
                "emoji": reaction_emoji_payload(canonical),
            }
        )
    assert dispatch == (dispatch_type, expected_payload)
    statement = session.execute.await_args.args[0]
    assert (canonical in statement.compile().params.values()) is (canonical is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_emoji", "event_type"),
    [
        (None, "MESSAGE_REACTION_REMOVE_ALL"),
        ("❤️", "MESSAGE_REACTION_REMOVE_EMOJI"),
    ],
)
async def test_local_reaction_clear_queues_the_same_aggregate_federation_event(
    monkeypatch: pytest.MonkeyPatch,
    raw_emoji: str | None,
    event_type: str,
) -> None:
    actor = SimpleNamespace(id=10, origin_domain="guild.example")
    guild = SimpleNamespace(id=12, origin_domain="guild.example")
    channel = SimpleNamespace(
        id=30,
        origin_domain="guild.example",
        guild_id=12,
        guild_domain="guild.example",
        type=0,
    )
    message = SimpleNamespace(id=20, origin_domain="guild.example")
    access = SimpleNamespace(guild=guild, channel=channel)
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                tuples=lambda: [(10, "guild.example", "❤")],
            )
        ),
        commit=AsyncMock(),
    )
    queued = AsyncMock()
    published = AsyncMock()
    monkeypatch.setattr(channel_api, "load_channel_access", AsyncMock(return_value=access))
    monkeypatch.setattr(
        channel_api,
        "lock_local_channel_mutation",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(channel_api, "require_channel_permissions", AsyncMock(return_value=0))
    monkeypatch.setattr(channel_api, "channel_message", AsyncMock(return_value=message))
    monkeypatch.setattr(channel_api, "mark_guild_activity", AsyncMock())
    monkeypatch.setattr(channel_api, "queue_guild_mutation", queued)
    monkeypatch.setattr(channel_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(channel_api, "publish_channel_dispatch", published)

    await channel_api._clear_reactions(
        EntityRef("30@guild.example"),
        EntityRef("20@guild.example"),
        raw_emoji,
        cast(Any, SimpleNamespace(user=actor)),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    expected_content: dict[str, object] = {
        "message": {"id": "20", "origin_domain": "guild.example"},
    }
    if raw_emoji is not None:
        expected_content["emoji"] = "❤"
    assert queued.await_args.args[4:] == (
        "guild.reaction.clear",
        expected_content,
    )
    assert queued.await_args.kwargs == {"channel": channel}
    assert published.await_args.args[2] == event_type
    assert published.await_args.args[3]["message_id"] == "20"
    if raw_emoji is not None:
        assert published.await_args.args[3]["reaction"] == "❤"


@pytest.mark.asyncio
async def test_local_bulk_delete_queues_one_aggregate_federation_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=10, origin_domain="guild.example")
    guild = SimpleNamespace(id=12, origin_domain="guild.example")
    channel = SimpleNamespace(
        id=30,
        origin_domain="guild.example",
        guild_id=12,
        guild_domain="guild.example",
        type=0,
        starter_message_id=None,
        starter_message_domain=None,
    )
    messages = [
        SimpleNamespace(
            id=20,
            origin_domain="guild.example",
            content="one",
            e2ee=None,
            deleted_at=None,
            flags=0,
        ),
        SimpleNamespace(
            id=21,
            origin_domain="member.example",
            content="two",
            e2ee=None,
            deleted_at=None,
            flags=0,
        ),
    ]
    access = SimpleNamespace(guild=guild, channel=channel)
    query_result = SimpleNamespace(tuples=lambda: [])
    session = SimpleNamespace(
        execute=AsyncMock(return_value=query_result),
        scalars=AsyncMock(return_value=messages),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    queued = AsyncMock()
    published = AsyncMock()
    monkeypatch.setattr(channel_api, "load_channel_access", AsyncMock(return_value=access))
    monkeypatch.setattr(channel_api, "lock_terminal_room", AsyncMock())
    monkeypatch.setattr(
        channel_api,
        "lock_local_channel_mutation",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(channel_api, "require_channel_permissions", AsyncMock(return_value=0))
    monkeypatch.setattr(
        channel_api,
        "queue_attachment_tombstones",
        AsyncMock(return_value=([], set())),
    )
    monkeypatch.setattr(channel_api, "queue_guild_mutation", queued)
    monkeypatch.setattr(channel_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(channel_api, "publish_channel_dispatch", published)

    await channel_api.bulk_delete_messages(
        EntityRef("30@guild.example"),
        MessageBulkDelete(
            message_ids=[
                EntityRef("20@guild.example"),
                EntityRef("21@member.example"),
            ]
        ),
        cast(Any, SimpleNamespace(user=actor)),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert queued.await_count == 1
    assert queued.await_args.args[4] == "guild.message.bulk_delete"
    assert queued.await_args.args[5]["messages"] == [
        {"id": "20", "origin_domain": "guild.example"},
        {"id": "21", "origin_domain": "member.example"},
    ]
    assert queued.await_args.kwargs == {"channel": channel}
    assert published.await_args.args[2] == "MESSAGE_DELETE_BULK"
    assert published.await_args.args[3]["ids"] == queued.await_args.args[5]["messages"]


@pytest.mark.asyncio
async def test_reaction_summaries_merge_canonical_aliases() -> None:
    message_ref = (20, "home.example")
    custom_alias = "<:lantern:75512661369970689@HOME.EXAMPLE.>"
    custom = "<:lantern:75512661369970689@home.example>"
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    all=lambda: [
                        (*message_ref, "❤️", 1),
                        (*message_ref, "❤", 2),
                        (*message_ref, custom_alias, 3),
                    ]
                ),
                SimpleNamespace(
                    all=lambda: [
                        (*message_ref, "❤️"),
                        (*message_ref, custom_alias),
                    ]
                ),
            ]
        )
    )

    payloads = await reaction_payloads_for_messages(
        cast(Any, session),
        {message_ref},
        viewer=cast(Any, SimpleNamespace(id=7, origin_domain="viewer.example")),
    )

    assert payloads[message_ref] == ({"❤": 3, custom: 3}, ["❤", custom])


def test_reaction_gateway_payload_has_discord_shape_and_composite_aliases() -> None:
    emoji = "<a:lantern:75512661369970689@emoji.example>"

    payload = reaction_event_payload(
        message_id=20,
        message_domain="guild.example",
        channel_id=30,
        channel_domain="guild.example",
        user_id=40,
        user_domain="member.example",
        emoji=emoji,
        guild_id=10,
        guild_domain="guild.example",
        message_author_id=50,
        message_author_domain="author.example",
        removed=True,
    )

    assert payload["id"] == payload["message_id"] == "20"
    assert payload["reaction"] == emoji
    assert payload["emoji"] == {
        "id": "75512661369970689",
        "origin_domain": "emoji.example",
        "name": "lantern",
        "animated": True,
    }
    assert payload["burst"] is False
    assert payload["burst_colors"] == []
    assert payload["type"] == 0
    assert payload["removed"] is True

    with pytest.raises(ValueError, match="guild reference"):
        reaction_event_payload(
            message_id=20,
            message_domain="guild.example",
            channel_id=30,
            channel_domain="guild.example",
            user_id=40,
            user_domain="member.example",
            emoji="🏮",
            guild_id=10,
        )


@pytest.mark.asyncio
async def test_guild_home_queues_remote_actor_reaction_for_authority_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = GuildReactionProxyRequest.model_validate(reaction_request())
    actor = SimpleNamespace(
        id=10,
        origin_domain="member.example",
        is_local=False,
        account_type="human",
    )
    guild = SimpleNamespace(
        id=12,
        origin_domain="guild.example",
        owner_id=11,
        owner_domain="guild.example",
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
        author_id=22,
        author_domain="author.example",
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
        return None

    session.get.side_effect = get
    session.scalar.return_value = message.id
    queue_mutation = AsyncMock()
    rate_limit = AsyncMock()
    admission = AsyncMock()
    interaction_gate = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", rate_limit)
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", admission)
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        federation_api,
        "require_permissions",
        AsyncMock(return_value=Permission.ADD_REACTIONS),
    )
    monkeypatch.setattr(federation_api, "queue_guild_mutation", queue_mutation)
    monkeypatch.setattr(
        federation_api,
        "require_member_interactions_allowed",
        interaction_gate,
    )
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
    assert rate_limit.await_args.args[1:] == ("member.example", "guild-reaction-mutation")
    assert rate_limit.await_args.kwargs == {
        "capacity": 3_000,
        "refill_per_minute": 3_000,
    }
    admission.assert_awaited_once_with(session, actor)
    interaction_gate.assert_awaited_once_with(
        session,
        guild,
        actor,
        Permission.ADD_REACTIONS,
    )
    assert queue_mutation.await_args.args[3] is actor
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
async def test_remote_guild_reaction_includes_expression_security_context(
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
    proof = {"emoji.example": {"type": "expression.use.authorization"}}

    response = await channel_api.proxy_remote_guild_reaction(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="member.example")),
        cast(Any, access),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(resolve=lambda _domain: (77, "author.example"))),
        "<:wave:88@emoji.example>",
        remove=False,
        expression_authorizations=proof,
        application_ref=(99, "app.example"),
    )

    assert response.status_code == 204
    assert signed.await_args.kwargs["payload"]["expression_authorizations"] == proof
    assert signed.await_args.kwargs["payload"]["application_id"] == "99@app.example"


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
