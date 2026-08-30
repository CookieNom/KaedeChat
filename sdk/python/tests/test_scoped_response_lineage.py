from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot import (
    AutoModAction,
    Client,
    EntityRef,
    ForwardedMessageReference,
    Message,
    WorkerState,
)


TARGET = "https://chat.example"
GUILD = EntityRef(10, "chat.example")
CHANNEL = EntityRef(30, "chat.example")
EVENT = EntityRef(20, "chat.example")


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def scheduled_event_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "20",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "channel_id": "30",
        "channel_domain": "chat.example",
        "creator_id": "2",
        "creator_domain": "apps.example",
        "creator": {
            "id": "2",
            "origin_domain": "apps.example",
            "username": "calendar-bot",
            "bot": True,
        },
        "name": "Community call",
        "scheduled_start_time": "2026-09-01T18:00:00+00:00",
        "scheduled_end_time": None,
        "privacy_level": 2,
        "status": 1,
        "entity_type": 2,
    }
    payload.update(overrides)
    return payload


def auto_mod_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "40",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "name": "Block spam",
        "creator_id": "2",
        "creator_domain": "apps.example",
        "event_type": "message_send",
        "trigger_type": "spam",
        "trigger_metadata": {},
        "actions": [{"type": "block_message", "metadata": {}}],
        "enabled": True,
        "exempt_roles": [],
        "exempt_channels": [],
        "version": 1,
        "created_at": "2026-08-29T00:00:00+00:00",
        "updated_at": "2026-08-29T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def webhook_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "50",
        "guild_domain": "chat.example",
        "guild_id": "10",
        "channel_id": "30",
        "channel_domain": "chat.example",
        "name": "Builds",
    }
    payload.update(overrides)
    return payload


def message_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "60",
        "origin_domain": "chat.example",
        "channel_id": "30",
        "channel_domain": "chat.example",
        "content": "hello",
        "created_at": "2026-08-29T00:00:00+00:00",
        "attachments": [],
    }
    payload.update(overrides)
    return payload


def follow_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "70",
        "ref": "70@chat.example",
        "source_channel_id": "30",
        "source_channel_domain": "chat.example",
        "target_channel_id": "31",
        "target_channel_domain": "chat.example",
        "creator_id": "2",
        "creator_domain": "apps.example",
        "active": True,
        "federated": False,
        "generation": None,
        "lifecycle_state": "active",
        "name": None,
        "avatar_hash": None,
        "created_at": "2026-08-29T00:00:00+00:00",
        "updated_at": "2026-08-29T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def stage_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "60",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "channel_id": "30",
        "channel_domain": "chat.example",
        "topic": "Town hall",
        "privacy_level": 2,
        "guild_scheduled_event_id": "20",
        "guild_scheduled_event_domain": "chat.example",
    }
    payload.update(overrides)
    return payload


def tracker_lane_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "80",
        "origin_domain": "chat.example",
        "channel_id": "30",
        "channel_domain": "chat.example",
        "name": "Todo",
        "position": 0,
    }
    payload.update(overrides)
    return payload


def tracker_task_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "90",
        "origin_domain": "chat.example",
        "channel_id": "30",
        "channel_domain": "chat.example",
        "lane_id": "80",
        "lane_domain": "chat.example",
        "number": 1,
        "key": "OPS-1",
        "title": "Ship it",
        "creator": {
            "id": "70",
            "origin_domain": "users.example",
            "username": "alice",
        },
    }
    payload.update(overrides)
    return payload


def application_asset_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "100",
        "application_ref": "1@apps.example",
        "kind": "icon",
        "name": "Logo",
        "media_hash": "a" * 64,
        "content_type": "image/png",
        "width": 128,
        "height": 128,
        "version": 1,
        "created_at": "2026-08-29T00:00:00+00:00",
        "updated_at": "2026-08-29T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def emoji_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "110",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "name": "wave",
        "roles": [],
    }
    payload.update(overrides)
    return payload


def sticker_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "120",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "name": "hello",
        "tags": ["wave"],
    }
    payload.update(overrides)
    return payload


def sound_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "130",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "name": "Air horn",
        "media_hash": "b" * 64,
        "content_type": "audio/ogg",
        "volume": 1,
        "duration_ms": 500,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_scheduled_event_responses_reject_colliding_authority_and_ids() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            scheduled_event_payload(origin_domain="evil.example"),
            scheduled_event_payload(id="21"),
            scheduled_event_payload(
                channel_id="30",
                channel_domain="evil.example",
            ),
        ]
    )

    with pytest.raises(ValueError, match="requested resource"):
        await bot.fetch_scheduled_event(GUILD, EVENT, target=TARGET)
    with pytest.raises(ValueError, match="requested resource"):
        await bot.edit_scheduled_event(
            GUILD,
            EVENT,
            target=TARGET,
            name="Renamed",
        )
    with pytest.raises(ValueError, match="requested lineage"):
        await bot.create_scheduled_event(
            GUILD,
            "Community call",
            datetime.now(UTC) + timedelta(hours=1),
            entity_type=2,
            channel=CHANNEL,
            target=TARGET,
        )


@pytest.mark.asyncio
async def test_scheduled_event_users_bind_exact_event_and_member_guild() -> None:
    user = {
        "id": "70",
        "origin_domain": "users.example",
        "username": "alice",
    }
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            [
                {
                    "guild_scheduled_event_id": "20",
                    "guild_scheduled_event_domain": "evil.example",
                    "user": user,
                    "subscribed_at": "2026-08-29T00:00:00+00:00",
                }
            ],
            [
                {
                    "guild_scheduled_event_id": "20",
                    "guild_scheduled_event_domain": "chat.example",
                    "user": user,
                    "member": {
                        "guild_id": "10",
                        "guild_domain": "evil.example",
                        "user": user,
                        "nickname": None,
                        "joined_at": "2026-08-29T00:00:00+00:00",
                    },
                    "subscribed_at": "2026-08-29T00:00:00+00:00",
                }
            ],
        ]
    )

    with pytest.raises(ValueError, match="requested lineage"):
        await bot.scheduled_event_users(GUILD, EVENT, target=TARGET)
    with pytest.raises(ValueError, match="requested lineage"):
        await bot.scheduled_event_users(GUILD, EVENT, target=TARGET)


@pytest.mark.asyncio
async def test_auto_mod_responses_reject_colliding_authority_ids_and_channels() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            auto_mod_payload(origin_domain="evil.example"),
            auto_mod_payload(id="41"),
            [auto_mod_payload(exempt_channels=["30@evil.example"])],
        ]
    )

    with pytest.raises(ValueError, match="requested resource"):
        await bot.fetch_auto_mod_rule(GUILD, 40, target=TARGET)
    with pytest.raises(ValueError, match="requested resource"):
        await bot.edit_auto_mod_rule(GUILD, 40, target=TARGET, name="Renamed")
    with pytest.raises(ValueError, match="requested resource"):
        await bot.auto_mod_rules(GUILD, target=TARGET)

    bot.request.reset_mock()
    with pytest.raises(ValueError, match="chat.example authority"):
        await bot.create_auto_mod_rule(
            GUILD,
            "Alerts",
            "spam",
            [
                AutoModAction(
                    "send_alert_message",
                    channel_ref=EntityRef(30, "evil.example"),
                )
            ],
            target=TARGET,
        )
    bot.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_responses_reject_colliding_authority_ids_and_channels() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            webhook_payload(guild_domain="evil.example"),
            webhook_payload(id="51"),
            [webhook_payload(channel_domain="evil.example")],
            webhook_payload(guild_domain="evil.example"),
        ]
    )

    with pytest.raises(ValueError, match="requested"):
        await bot.fetch_webhook(GUILD, 50, target=TARGET)
    with pytest.raises(ValueError, match="requested"):
        await bot.edit_webhook(GUILD, 50, target=TARGET, name="Renamed")
    with pytest.raises(ValueError, match="requested guild or channel"):
        await bot.channel_webhooks(GUILD, CHANNEL, target=TARGET)
    with pytest.raises(ValueError, match="requested resource"):
        await bot.fetch_webhook_with_token(50, "secret", target=TARGET)


@pytest.mark.asyncio
async def test_webhook_message_responses_bind_token_to_exact_lineage() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            message_payload(webhook_id="51", webhook_domain="chat.example"),
            message_payload(
                id="61",
                webhook_id="50",
                webhook_domain="chat.example",
            ),
            message_payload(
                channel_id="31",
                webhook_id="50",
                webhook_domain="chat.example",
            ),
        ]
    )

    with pytest.raises(ValueError, match="webhook message response"):
        await bot.execute_webhook(50, "secret", "hello", target=TARGET)
    with pytest.raises(ValueError, match="webhook message response"):
        await bot.fetch_webhook_message(
            50,
            "secret",
            EntityRef(60, "chat.example"),
            target=TARGET,
        )
    with pytest.raises(ValueError, match="webhook message response"):
        await bot.edit_webhook_message(
            50,
            "secret",
            EntityRef(60, "chat.example"),
            target=TARGET,
            thread_id=CHANNEL,
            content="edited",
        )


@pytest.mark.asyncio
async def test_poll_finalize_and_crosspost_bind_exact_requested_message() -> None:
    bot = client()
    requested = EntityRef(60, "chat.example")
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            message_payload(id="61"),
            message_payload(channel_id="31"),
        ]
    )

    with pytest.raises(ValueError, match="poll finalization response"):
        await bot.finalize_poll(CHANNEL, requested, target=TARGET)
    with pytest.raises(ValueError, match="crosspost response"):
        await bot.crosspost_message(CHANNEL, requested, target=TARGET)


@pytest.mark.asyncio
async def test_forward_resolve_models_snapshot_refs_and_binds_saved_lineage() -> None:
    bot = client()
    destination = Message.from_payload(
        bot,
        TARGET,
        message_payload(
            id="80",
            forwarded_message_id="90",
            forwarded_message_domain="source.example",
            forwarded_channel_id="40",
            forwarded_channel_domain="source.example",
            forward_snapshot={"content": "saved"},
        ),
    )
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "source_channel_ref": "40@source.example",
                "source_message_ref": "90@source.example",
            },
            {
                "source_channel_ref": "40@source.example",
                "source_message_ref": "91@source.example",
            },
        ]
    )

    resolved = await destination.resolve_forwarded()
    assert resolved == ForwardedMessageReference(
        EntityRef(40, "source.example"),
        EntityRef(90, "source.example"),
    )
    with pytest.raises(ValueError, match="saved source lineage"):
        await destination.resolve_forwarded()


@pytest.mark.asyncio
async def test_announcement_follow_responses_are_strictly_source_bound() -> None:
    bot = client()
    target_channel = EntityRef(31, "chat.example")
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            follow_payload(source_channel_domain="evil.example"),
            [
                follow_payload(id="71", ref="71@chat.example"),
                follow_payload(),
            ],
        ]
    )

    with pytest.raises(ValueError, match="requested lineage"):
        await bot.follow_announcement_channel(
            CHANNEL,
            target_channel,
            target=TARGET,
        )
    with pytest.raises(ValueError, match="duplicate or unordered"):
        await bot.announcement_follows(CHANNEL, target=TARGET)


@pytest.mark.asyncio
async def test_interaction_message_response_cannot_change_target_authority() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=message_payload(
            origin_domain="evil.example",
            channel_domain="evil.example",
            interaction_id="90",
            response_id="70",
        )
    )

    with pytest.raises(ValueError, match="changed its authority"):
        await bot.fetch_original_interaction_response(90, target=TARGET)


@pytest.mark.asyncio
async def test_stage_responses_reject_colliding_authority_and_event_ids() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            stage_payload(channel_domain="evil.example"),
            stage_payload(guild_scheduled_event_id="21"),
            stage_payload(origin_domain="evil.example"),
        ]
    )

    with pytest.raises(ValueError, match="requested channel"):
        await bot.fetch_stage_instance(CHANNEL, target=TARGET)
    with pytest.raises(ValueError, match="requested scheduled event"):
        await bot.create_stage_instance(
            CHANNEL,
            "Town hall",
            scheduled_event=EVENT,
            target=TARGET,
        )
    with pytest.raises(ValueError, match="requested resource"):
        await bot.edit_stage_instance(CHANNEL, topic="Renamed", target=TARGET)


@pytest.mark.asyncio
async def test_tracker_responses_and_inputs_bind_channel_authority() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "channel_id": "30",
                "channel_domain": "chat.example",
                "key_prefix": "OPS",
                "lanes": [tracker_lane_payload(origin_domain="evil.example")],
                "tasks": [],
            },
            tracker_task_payload(id="91"),
        ]
    )

    with pytest.raises(ValueError, match="requested resource"):
        await bot.fetch_tracker(CHANNEL, target=TARGET)
    with pytest.raises(ValueError, match="requested resource"):
        await bot.edit_tracker_task(
            CHANNEL,
            EntityRef(90, "chat.example"),
            target=TARGET,
            version="task-v1",
            title="Renamed",
        )

    bot.request.reset_mock()
    with pytest.raises(ValueError, match="chat.example authority"):
        await bot.move_tracker_task(
            CHANNEL,
            EntityRef(90, "chat.example"),
            EntityRef(80, "evil.example"),
            1,
            target=TARGET,
            version="task-v1",
        )
    bot.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_application_asset_response_binds_exact_application_and_id() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            application_asset_payload(application_ref="1@evil.example"),
            application_asset_payload(id="101"),
        ]
    )

    with pytest.raises(ValueError, match="requested application"):
        await bot.fetch_application_asset(100, target="https://apps.example")
    with pytest.raises(ValueError, match="requested resource"):
        await bot.edit_application_asset(
            100,
            target="https://apps.example",
            name="Renamed",
        )


@pytest.mark.asyncio
async def test_expression_and_sound_responses_bind_guild_and_resource_ids() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            emoji_payload(origin_domain="evil.example"),
            sticker_payload(id="121"),
            sound_payload(emoji_id="110", emoji_domain="evil.example"),
        ]
    )

    with pytest.raises(ValueError, match="requested resource"):
        await bot.fetch_emoji(GUILD, 110, target=TARGET)
    with pytest.raises(ValueError, match="requested resource"):
        await bot.edit_sticker(
            GUILD,
            120,
            target=TARGET,
            name="renamed",
        )
    with pytest.raises(ValueError, match="requested resource"):
        await bot.fetch_soundboard_sound(
            GUILD,
            EntityRef(130, "chat.example"),
            target=TARGET,
        )


@pytest.mark.asyncio
async def test_default_sound_responses_accept_target_authority_and_reject_collision() -> (
    None
):
    valid = sound_payload()
    valid.pop("guild_id")
    valid.pop("guild_domain")
    colliding = {**valid, "origin_domain": "evil.example"}
    bot = client()
    bot.request = AsyncMock(side_effect=[[valid], [colliding]])  # type: ignore[method-assign]

    sounds = await bot.default_soundboard_sounds(target=TARGET)
    assert [sound.ref for sound in sounds] == [EntityRef(130, "chat.example")]
    with pytest.raises(ValueError, match="requested resource"):
        await bot.default_soundboard_sounds(target=TARGET)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "AUTO_MODERATION_RULE_UPDATE",
            auto_mod_payload(origin_domain="evil.example"),
        ),
        (
            "GUILD_SCHEDULED_EVENT_UPDATE",
            scheduled_event_payload(origin_domain="evil.example"),
        ),
        (
            "TRACKER_LANE_UPDATE",
            {
                "channel_id": "30",
                "channel_domain": "chat.example",
                "lane": tracker_lane_payload(origin_domain="evil.example"),
            },
        ),
        ("GUILD_EMOJI_UPDATE", emoji_payload(origin_domain="evil.example")),
        ("GUILD_STICKER_UPDATE", sticker_payload(origin_domain="evil.example")),
        (
            "GUILD_SOUNDBOARD_SOUND_UPDATE",
            sound_payload(origin_domain="evil.example"),
        ),
    ],
)
async def test_gateway_dispatch_rejects_colliding_resource_authorities(
    event_type: str,
    payload: dict[str, object],
) -> None:
    bot = client()
    seen: list[object] = []

    async def record(event: object) -> None:
        seen.append(event)

    bot.listen(event_type)(record)
    with pytest.raises(ValueError, match="requested resource"):
        await bot.dispatch(
            event_type,
            payload,
            target=TARGET,
            topic="guild:chat.example:10",
            sequence=1,
        )
    assert seen == []


@pytest.mark.asyncio
async def test_gateway_dispatch_rejects_colliding_guild_topic_authority() -> None:
    bot = client()
    payload = scheduled_event_payload(
        guild_id="10",
        guild_domain="evil.example",
        origin_domain="evil.example",
        channel_domain="evil.example",
    )

    with pytest.raises(ValueError, match="subscribed guild authority"):
        await bot.dispatch(
            "GUILD_SCHEDULED_EVENT_UPDATE",
            payload,
            target=TARGET,
            topic="guild:chat.example:10",
            sequence=1,
        )
