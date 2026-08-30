from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

import app.api.bots as bots_api
import app.api.interactions as interactions_api
import app.api.stage_instances as stage_api
from app.api.scheduled_events import ScheduledEventPatch
from app.api.voice import require_bot_voice_member_channel_access
from app.api.webhooks import WebhookPatch
from app.bots.installations import installation_accessible_channel, installation_allows_channel
from app.core.types import EntityRef
from app.db.models import Channel as ChannelModel
from app.db.models import Guild as GuildModel
from app.db.models import User as UserModel
from app.db.models import Webhook
from app.voice.schemas import CurrentUserVoiceStateUpdate
from app.voice.state import room_state_key, voice_user_room_key


def guild() -> SimpleNamespace:
    return SimpleNamespace(id=11, origin_domain="chat.example")


def installation(*restrictions: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=70,
        guild_id=11,
        guild_domain="chat.example",
        application_domain="apps.remote",
        channel_restrictions=list(restrictions),
        granted_scopes=["webhooks.manage"],
    )


def channel(
    channel_id: int,
    *,
    parent_id: int | None = None,
    domain: str = "chat.example",
    channel_type: int = 0,
    guild_id: int = 11,
    guild_domain: str = "chat.example",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=channel_id,
        origin_domain=domain,
        guild_id=guild_id,
        guild_domain=guild_domain,
        unavailable=False,
        parent_id=parent_id,
        parent_domain=guild_domain if parent_id is not None else None,
        type=channel_type,
    )


@pytest.mark.asyncio
async def test_interaction_discovery_hides_restricted_channel_and_allows_category_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = SimpleNamespace(id=90)
    application = SimpleNamespace(id=80)
    grant = installation("5@chat.example")
    rows = [(command, application, grant)]
    category = channel(5, channel_type=4)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(all=Mock(return_value=rows))),
        get=AsyncMock(return_value=category),
    )
    monkeypatch.setattr(
        interactions_api,
        "command_payload",
        lambda *_args, **_kwargs: {
            "application_ref": "80@apps.remote",
            "name": "ping",
            "type": "chat_input",
        },
    )

    assert (
        await interactions_api._local_application_commands(
            cast(Any, session),
            cast(Any, guild()),
            channel=cast(Any, channel(14)),
        )
        == []
    )
    assert await interactions_api._local_application_commands(
        cast(Any, session),
        cast(Any, guild()),
        channel=cast(Any, channel(13, parent_id=5)),
    ) == [
        {
            "application_ref": "80@apps.remote",
            "name": "ping",
            "type": "chat_input",
        }
    ]


@pytest.mark.asyncio
async def test_local_interaction_admission_rejects_restricted_channel_before_command_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grant = installation("5@chat.example")
    application = SimpleNamespace(id=80, origin_domain="apps.remote")
    bot = SimpleNamespace(id=81, origin_domain="apps.remote")
    result = SimpleNamespace(one_or_none=Mock(return_value=(grant, application, bot)))
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        get=AsyncMock(return_value=channel(5, channel_type=4)),
    )
    command_lookup = AsyncMock(return_value=SimpleNamespace(id=90))
    monkeypatch.setattr(interactions_api, "guild_install_command", command_lookup)
    payload = interactions_api.InteractionCreate(
        application_ref="80@apps.remote",
        command_name="ping",
        command_id=90,
        integration_type="guild_install",
    )

    assert await interactions_api.guild_application_installation(
        cast(Any, session),
        cast(Any, guild()),
        cast(Any, channel(14)),
        payload,
        (80, "apps.remote"),
        None,
    ) == (None, None, None, None)
    command_lookup.assert_not_awaited()

    allowed = await interactions_api.guild_application_installation(
        cast(Any, session),
        cast(Any, guild()),
        cast(Any, channel(13, parent_id=5)),
        payload,
        (80, "apps.remote"),
        None,
    )
    allowed_values = cast(tuple[Any, Any, Any, Any], allowed)
    assert allowed_values[:3] == (grant, application, bot)
    assert allowed_values[3].id == 90
    command_lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_federated_interaction_authority_rejects_restricted_channel_before_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = SimpleNamespace(silenced=False, origin="users.example")
    settings = SimpleNamespace(domain="chat.example", federation_clock_skew_seconds=30)
    user = SimpleNamespace(id=20, origin_domain="users.example", account_type="human")
    target_guild = guild()
    grant = installation("5@chat.example")
    command = SimpleNamespace(id=90)
    payload = interactions_api.FederatedInteractionCreate(
        user_id="20",
        interaction=interactions_api.InteractionCreate(
            application_ref="80@apps.remote",
            command_name="ping",
            command_id=90,
            integration_type="guild_install",
        ),
        response_grant_id="igr_" + "g" * 40,
        response_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    create = AsyncMock(return_value={"id": "100", "status": "pending"})
    command_lookup = AsyncMock(return_value=command)
    monkeypatch.setattr(interactions_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(interactions_api, "guild_install_command", command_lookup)
    monkeypatch.setattr(interactions_api, "create_interaction", create)
    monkeypatch.setattr(interactions_api, "wake_application_target_deliveries", AsyncMock())

    async def invoke(target_channel: SimpleNamespace) -> dict[str, object]:
        category = channel(5, channel_type=4)

        async def get(model: type[object], ref: object, **_kwargs: object) -> object | None:
            if model is UserModel:
                return user
            if model is ChannelModel:
                return {
                    (target_channel.id, target_channel.origin_domain): target_channel,
                    (category.id, category.origin_domain): category,
                }.get(ref)
            if model is GuildModel:
                return target_guild
            return None

        session = SimpleNamespace(
            get=AsyncMock(side_effect=get),
            # Instance admission, response-grant lock, existing response,
            # then the guild installation used by command resolution.
            scalar=AsyncMock(side_effect=[None, None, None, grant]),
        )
        return await interactions_api.federation_create_interaction(
            target_channel.id,
            payload,
            cast(Any, principal),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, settings),
        )

    with pytest.raises(HTTPException) as hidden:
        await invoke(channel(14))
    assert hidden.value.status_code == 404
    assert cast(dict[str, str], hidden.value.detail) == {"code": "APPLICATION_COMMAND_NOT_FOUND"}
    create.assert_not_awaited()
    command_lookup.assert_not_awaited()

    result = await invoke(channel(13, parent_id=5))
    assert result == {"id": "100", "status": "pending"}
    create.assert_awaited_once()
    command_lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_installation_channel_access_is_parent_aware_and_composite() -> None:
    child = channel(13, parent_id=5)
    category = channel(5, channel_type=4)
    wrong_domain = channel(13, parent_id=5, domain="other.example")
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=lambda _model, ref, **_kwargs: {
                (13, "chat.example"): child,
                (5, "chat.example"): category,
                (13, "other.example"): wrong_domain,
            }.get(ref)
        )
    )

    assert (
        await installation_accessible_channel(
            cast(Any, session),
            cast(Any, installation("5@chat.example")),
            cast(Any, guild()),
            EntityRef("13@chat.example"),
        )
        is child
    )
    assert (
        await installation_accessible_channel(
            cast(Any, session),
            cast(Any, installation("5@chat.example")),
            cast(Any, guild()),
            EntityRef("13@other.example"),
        )
        is None
    )


@pytest.mark.asyncio
async def test_category_restriction_includes_thread_descendants_with_validated_ancestry() -> None:
    category = channel(5, channel_type=4)
    forum = channel(6, parent_id=5, channel_type=15)
    thread = channel(7, parent_id=6, channel_type=11)
    channels = {
        (forum.id, forum.origin_domain): forum,
        (category.id, category.origin_domain): category,
    }
    session = SimpleNamespace(
        get=AsyncMock(side_effect=lambda _model, ref, **_kwargs: channels.get(ref))
    )

    assert await installation_allows_channel(
        cast(Any, session),
        cast(Any, installation("5@chat.example")),
        cast(Any, thread),
    )
    assert not await installation_allows_channel(
        cast(Any, session),
        cast(Any, installation("8@chat.example")),
        cast(Any, thread),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_parent", "cross_guild_parent", "missing_category"])
async def test_category_restriction_fails_closed_for_invalid_thread_ancestry(
    failure: str,
) -> None:
    category = channel(5, channel_type=4)
    forum = channel(6, parent_id=5, channel_type=15)
    thread = channel(7, parent_id=6, channel_type=11)
    if failure == "missing_parent":
        channels: dict[tuple[int, str], SimpleNamespace] = {(5, "chat.example"): category}
    elif failure == "cross_guild_parent":
        channels = {
            (6, "chat.example"): channel(
                6,
                parent_id=5,
                channel_type=15,
                guild_id=12,
            ),
            (5, "chat.example"): category,
        }
    else:
        channels = {(6, "chat.example"): forum}
    session = SimpleNamespace(
        get=AsyncMock(side_effect=lambda _model, ref, **_kwargs: channels.get(ref))
    )

    assert not await installation_allows_channel(
        cast(Any, session),
        cast(Any, installation("5@chat.example")),
        cast(Any, thread),
    )


@pytest.mark.asyncio
async def test_installation_restriction_rejects_cross_guild_target_before_ancestor_lookup() -> None:
    target = channel(7, parent_id=6, channel_type=11, guild_id=12)
    session = SimpleNamespace(get=AsyncMock())

    assert not await installation_allows_channel(
        cast(Any, session),
        cast(Any, installation("7@chat.example")),
        cast(Any, target),
    )
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduled_event_list_and_reads_treat_channel_as_indivisible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_child = channel(13, parent_id=5)
    blocked = channel(14)
    category = channel(5, channel_type=4)
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=lambda _model, ref, **_kwargs: {
                (13, "chat.example"): parent_child,
                (14, "chat.example"): blocked,
                (5, "chat.example"): category,
            }.get(ref)
        )
    )
    grant = installation("5@chat.example")
    target_guild = guild()

    assert await bots_api._bot_scheduled_event_payload_allowed(
        cast(Any, session),
        cast(Any, target_guild),
        cast(Any, grant),
        {
            "entity_type": 2,
            "channel_id": "13",
            "channel_domain": "chat.example",
        },
    )
    assert not await bots_api._bot_scheduled_event_payload_allowed(
        cast(Any, session),
        cast(Any, target_guild),
        cast(Any, grant),
        {
            "entity_type": 1,
            "channel_id": "14",
            "channel_domain": "chat.example",
        },
    )
    assert await bots_api._bot_scheduled_event_payload_allowed(
        cast(Any, session),
        cast(Any, target_guild),
        cast(Any, grant),
        {"entity_type": 3, "channel_id": None, "channel_domain": None},
    )

    hidden_event = SimpleNamespace(
        entity_type=2,
        channel_id=14,
        channel_domain="chat.example",
    )
    monkeypatch.setattr(
        bots_api,
        "scheduled_event_for_guild",
        AsyncMock(return_value=hidden_event),
    )
    with pytest.raises(HTTPException) as hidden:
        await bots_api._require_bot_scheduled_event(
            cast(Any, session),
            cast(Any, target_guild),
            cast(Any, grant),
            EntityRef("90@chat.example"),
            not_found=True,
        )
    assert hidden.value.status_code == 404
    assert hidden.value.detail == {"code": "SCHEDULED_EVENT_NOT_FOUND"}


@pytest.mark.asyncio
async def test_scheduled_event_patch_checks_stored_and_new_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_guild = guild()
    grant = installation("13@chat.example", "14@chat.example")
    route_session = SimpleNamespace()
    current = AsyncMock(return_value=SimpleNamespace())
    requested = AsyncMock(return_value=channel(14))
    update = AsyncMock(return_value={"id": "90"})
    monkeypatch.setattr(
        bots_api,
        "installation_for_guild",
        AsyncMock(return_value=(target_guild, grant)),
    )
    monkeypatch.setattr(bots_api, "_require_bot_scheduled_event", current)
    monkeypatch.setattr(bots_api, "_require_bot_requested_channel", requested)
    monkeypatch.setattr(bots_api, "patch_scheduled_event", update)

    await bots_api.bot_patch_scheduled_event(
        EntityRef("11@chat.example"),
        EntityRef("90@chat.example"),
        ScheduledEventPatch(channel_id=EntityRef("14@chat.example")),
        cast(Any, SimpleNamespace(user=SimpleNamespace())),
        cast(Any, route_session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="chat.example")),
        None,
    )

    current.assert_awaited_once()
    assert current.await_args.kwargs["for_update"] is True
    requested.assert_awaited_once_with(
        route_session,
        target_guild,
        grant,
        EntityRef("14@chat.example"),
    )
    update.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("follower", [False, True])
async def test_webhook_lookup_hides_restricted_ordinary_and_follower_channels(
    monkeypatch: pytest.MonkeyPatch,
    follower: bool,
) -> None:
    target_guild = guild()
    grant = installation("13@chat.example")
    item = SimpleNamespace(
        id=7,
        guild_id=11,
        guild_domain="chat.example",
        channel_id=14,
        channel_domain="chat.example",
    )
    follow = SimpleNamespace(
        id=7,
        target_channel_id=14,
        target_channel_domain="chat.example",
    )
    blocked_channel = channel(14)

    async def get(model: type[object], _ref: object) -> object | None:
        if model is Webhook:
            return None if follower else item
        if model is ChannelModel:
            return blocked_channel
        return None

    monkeypatch.setattr(
        bots_api,
        "installation_for_guild_any_scope",
        AsyncMock(return_value=(target_guild, grant)),
    )
    monkeypatch.setattr(
        bots_api,
        "target_follower_webhook",
        AsyncMock(return_value=follow if follower else None),
    )
    with pytest.raises(HTTPException) as hidden:
        await bots_api.bot_guild_webhook(
            cast(Any, SimpleNamespace(get=AsyncMock(side_effect=get))),
            cast(Any, SimpleNamespace(domain="chat.example")),
            cast(Any, SimpleNamespace()),
            EntityRef("11@chat.example"),
            7,
        )
    assert hidden.value.status_code == 404
    assert hidden.value.detail == {"code": "WEBHOOK_NOT_FOUND"}


@pytest.mark.asyncio
async def test_webhook_move_checks_both_current_and_new_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_guild = guild()
    grant = installation("13@chat.example")
    channels = {
        (13, "chat.example"): channel(13),
        (14, "chat.example"): channel(14),
    }
    session = SimpleNamespace(get=AsyncMock(side_effect=lambda _model, ref: channels.get(ref)))
    principal = SimpleNamespace(
        user=SimpleNamespace(),
        scopes=frozenset({"webhooks.manage"}),
    )
    patch = AsyncMock()
    monkeypatch.setattr(
        bots_api,
        "bot_guild_webhook",
        AsyncMock(
            return_value=(
                target_guild,
                grant,
                EntityRef("7@chat.example"),
                channels[(13, "chat.example")],
            )
        ),
    )
    monkeypatch.setattr(bots_api, "patch_webhook", patch)
    with pytest.raises(HTTPException) as hidden_target:
        await bots_api.bot_update_webhook(
            EntityRef("11@chat.example"),
            7,
            WebhookPatch(channel_id=EntityRef("14@chat.example")),
            cast(Any, principal),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="chat.example")),
            None,
        )
    assert hidden_target.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    patch.assert_not_awaited()

    current_hidden = AsyncMock(
        side_effect=HTTPException(
            status_code=404,
            detail={"code": "WEBHOOK_NOT_FOUND"},
        )
    )
    monkeypatch.setattr(bots_api, "bot_guild_webhook", current_hidden)
    with pytest.raises(HTTPException):
        await bots_api.bot_update_webhook(
            EntityRef("11@chat.example"),
            7,
            WebhookPatch(channel_id=EntityRef("13@chat.example")),
            cast(Any, principal),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="chat.example")),
            None,
        )
    patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_e2ee_rejects_a_hidden_participation_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_guild = guild()
    grant = installation("13@chat.example")
    current_channel = channel(13)
    operation = AsyncMock()
    monkeypatch.setattr(
        bots_api,
        "bot_guild_webhook",
        AsyncMock(
            return_value=(
                target_guild,
                grant,
                EntityRef("7@chat.example"),
                current_channel,
            )
        ),
    )
    monkeypatch.setattr(bots_api, "get_webhook_e2ee_participation", operation)
    with pytest.raises(HTTPException) as denied:
        await bots_api.bot_get_webhook_e2ee_participation(
            EntityRef("11@chat.example"),
            7,
            EntityRef("14@chat.example"),
            cast(Any, SimpleNamespace(user=SimpleNamespace())),
            cast(
                Any,
                SimpleNamespace(
                    get=AsyncMock(return_value=channel(14)),
                ),
            ),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="chat.example")),
        )
    assert denied.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_moderation_fences_source_destination_and_allows_disconnected() -> None:
    target_guild = guild()
    identity = "90@users.example"
    source_room = "g.11.13"
    channels = {
        (13, "chat.example"): channel(13, channel_type=2),
        (14, "chat.example"): channel(14, channel_type=13),
    }
    session = SimpleNamespace(get=AsyncMock(side_effect=lambda _model, ref: channels.get(ref)))
    pointers = {
        voice_user_room_key("chat.example", identity, guild_id=11): source_room,
    }
    occupants = {
        (
            room_state_key("occupancy", "chat.example", source_room),
            identity,
        ): json.dumps(
            {
                "identity": identity,
                "user_id": "90",
                "user_domain": "users.example",
                "room": source_room,
                "guild_id": "11",
                "channel_id": "13",
                "joined_at": 1,
            }
        ),
    }
    redis = SimpleNamespace(
        get=AsyncMock(side_effect=lambda key: pointers.get(key)),
        hget=AsyncMock(side_effect=lambda key, field: occupants.get((key, field))),
    )

    with pytest.raises(HTTPException) as hidden_destination:
        await require_bot_voice_member_channel_access(
            cast(Any, session),
            cast(Any, redis),
            cast(Any, SimpleNamespace(domain="chat.example")),
            cast(Any, target_guild),
            EntityRef("90@users.example"),
            cast(Any, installation("13@chat.example")),
            target_channel_ref=EntityRef("14@chat.example"),
        )
    assert hidden_destination.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}

    with pytest.raises(HTTPException) as hidden_source:
        await require_bot_voice_member_channel_access(
            cast(Any, session),
            cast(Any, redis),
            cast(Any, SimpleNamespace(domain="chat.example")),
            cast(Any, target_guild),
            EntityRef("90@users.example"),
            cast(Any, installation("14@chat.example")),
            target_channel_ref=EntityRef("14@chat.example"),
        )
    assert hidden_source.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}

    pointers.clear()
    await require_bot_voice_member_channel_access(
        cast(Any, session),
        cast(Any, redis),
        cast(Any, SimpleNamespace(domain="chat.example")),
        cast(Any, target_guild),
        EntityRef("90@users.example"),
        cast(Any, installation("13@chat.example")),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [False, True])
async def test_stage_voice_state_hides_current_channel_after_restriction_change(
    monkeypatch: pytest.MonkeyPatch,
    mutation: bool,
) -> None:
    target_guild = guild()
    stage_channel = channel(14, channel_type=13)
    actor = SimpleNamespace(id=80, origin_domain="apps.remote")
    target = SimpleNamespace(id=80, origin_domain="apps.remote")
    monkeypatch.setattr(
        stage_api,
        "stage_guild_for_actor",
        AsyncMock(return_value=target_guild),
    )
    monkeypatch.setattr(
        stage_api,
        "connected_stage_occupant",
        AsyncMock(return_value=(SimpleNamespace(), stage_channel, target)),
    )
    monkeypatch.setattr(stage_api, "require_permissions", AsyncMock())

    if mutation:
        with pytest.raises(HTTPException) as denied:
            await stage_api.update_local_stage_voice_state(
                cast(Any, SimpleNamespace()),
                cast(Any, SimpleNamespace()),
                cast(Any, SimpleNamespace()),
                EntityRef("11@chat.example"),
                cast(Any, actor),
                EntityRef("80@apps.remote"),
                CurrentUserVoiceStateUpdate(suppress=True),
                current_user=True,
                acting_installation=cast(
                    Any,
                    installation("13@chat.example"),
                ),
            )
        assert denied.value.status_code == 403
        assert denied.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    else:
        with pytest.raises(HTTPException) as hidden:
            await stage_api.get_local_stage_voice_state(
                cast(Any, SimpleNamespace()),
                cast(Any, SimpleNamespace()),
                cast(Any, SimpleNamespace()),
                EntityRef("11@chat.example"),
                cast(Any, actor),
                EntityRef("80@apps.remote"),
                acting_installation=cast(
                    Any,
                    installation("13@chat.example"),
                ),
            )
        assert hidden.value.status_code == 404
        assert hidden.value.detail == {"code": "VOICE_STATE_NOT_FOUND"}
