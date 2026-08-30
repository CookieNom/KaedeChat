from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot import (
    Client,
    EntityRef,
    ScheduledEvent,
    ScheduledEventNWeekday,
    ScheduledEventRecurrenceRule,
    ScheduledEventUser,
    ScheduledEventUserEvent,
    WorkerState,
)

TARGET = "https://chat.example"
GUILD = EntityRef(10, "chat.example")
EVENT = EntityRef(20, "chat.example")
CHANNEL = EntityRef(30, "chat.example")


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def event_payload(**overrides: object) -> dict[str, object]:
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
            "display_name": "Calendar Bot",
            "bot": True,
        },
        "name": "Community call",
        "description": "Monthly project update",
        "scheduled_start_time": "2026-09-01T18:00:00+00:00",
        "scheduled_end_time": None,
        "privacy_level": 2,
        "status": 1,
        "entity_type": 2,
        "entity_id": None,
        "entity_domain": None,
        "entity_metadata": None,
        "image": None,
        "user_count": 3,
        "created_at": "2026-08-27T12:00:00+00:00",
        "updated_at": "2026-08-27T12:00:00+00:00",
        "version": "2026-08-27T12:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def last_await(mock: AsyncMock) -> Any:
    call = mock.await_args
    assert call is not None
    return call


@pytest.mark.asyncio
async def test_voice_event_create_and_model_helpers_use_typed_routes() -> None:
    bot = client()
    start = datetime.now(UTC) + timedelta(hours=2)
    bot.request = AsyncMock(return_value=event_payload())  # type: ignore[method-assign]

    event = await bot.create_scheduled_event(
        GUILD,
        " Community call ",
        start,
        entity_type=2,
        target=TARGET,
        channel=CHANNEL,
        description=" Monthly project update ",
        reason="publish calendar",
    )

    assert isinstance(event, ScheduledEvent)
    assert event.channel_ref == CHANNEL
    assert event.creator is not None and event.creator.bot
    assert event.user_count == 3
    assert last_await(bot.request).args[:2] == (
        "POST",
        "/api/v1/bots/guilds/10@chat.example/scheduled-events",
    )
    assert last_await(bot.request).kwargs["json"] == {
        "channel_id": "30@chat.example",
        "entity_metadata": None,
        "name": "Community call",
        "privacy_level": 2,
        "scheduled_start_time": start.isoformat(),
        "scheduled_end_time": None,
        "description": "Monthly project update",
        "entity_type": 2,
    }
    assert last_await(bot.request).kwargs["headers"] == {
        "X-Audit-Log-Reason": "publish calendar"
    }

    bot.request.reset_mock()
    bot.request.return_value = event_payload(status=2)
    updated = await event.edit(status=2, reason="starting now")
    assert updated.status == 2
    assert last_await(bot.request).args[:2] == (
        "PATCH",
        "/api/v1/bots/guilds/10@chat.example/scheduled-events/20@chat.example",
    )
    assert last_await(bot.request).kwargs["json"] == {"status": 2}

    bot.request.reset_mock()
    bot.request.return_value = None
    await event.delete(reason="calendar cleanup")
    assert last_await(bot.request).args[0] == "DELETE"


@pytest.mark.asyncio
async def test_stage_event_create_uses_stage_entity_type_and_channel() -> None:
    bot = client()
    start = datetime.now(UTC) + timedelta(hours=2)
    bot.request = AsyncMock(return_value=event_payload(entity_type=1))  # type: ignore[method-assign]

    event = await bot.create_scheduled_event(
        GUILD,
        "Stage town hall",
        start,
        entity_type=1,
        target=TARGET,
        channel=CHANNEL,
    )

    assert event.entity_type == 1
    assert last_await(bot.request).kwargs["json"]["entity_type"] == 1
    assert last_await(bot.request).kwargs["json"]["channel_id"] == "30@chat.example"


@pytest.mark.asyncio
async def test_recurrence_rule_uses_discord_enum_and_round_trips() -> None:
    bot = client()
    start = datetime.now(UTC) + timedelta(hours=2)
    rule = ScheduledEventRecurrenceRule(
        start=start,
        frequency=1,
        by_n_weekday=(ScheduledEventNWeekday(1, 2),),
    )
    bot.request = AsyncMock(return_value=event_payload(recurrence_rule=rule.to_dict()))  # type: ignore[method-assign]

    event = await bot.create_scheduled_event(
        GUILD,
        "Monthly call",
        start,
        entity_type=2,
        channel=CHANNEL,
        target=TARGET,
        recurrence_rule=rule,
    )

    assert event.recurrence_rule == rule
    assert last_await(bot.request).kwargs["json"]["recurrence_rule"]["frequency"] == 1
    with pytest.raises(ValueError, match="only weekly"):
        ScheduledEventRecurrenceRule(start=start, frequency=1, interval=2)
    with pytest.raises(ValueError, match="frequency"):
        ScheduledEventRecurrenceRule(start=start, frequency=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="interval"):
        ScheduledEventRecurrenceRule(
            start=start,
            frequency=2,
            interval=True,  # type: ignore[arg-type]
            by_weekday=(1,),
        )
    invalid = rule.to_dict()
    invalid["frequency"] = True
    with pytest.raises(ValueError, match="integer"):
        ScheduledEventRecurrenceRule.from_payload(invalid)


@pytest.mark.asyncio
async def test_scheduled_event_sdk_rejects_boolean_enums() -> None:
    bot = client()
    start = datetime.now(UTC) + timedelta(hours=2)
    bot.request = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="entity_type"):
        await bot.create_scheduled_event(
            GUILD,
            "Invalid",
            start,
            entity_type=True,  # type: ignore[arg-type]
            channel=CHANNEL,
        )
    with pytest.raises(ValueError, match="entity_type"):
        await bot.edit_scheduled_event(
            GUILD,
            EVENT,
            entity_type=True,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="status"):
        await bot.edit_scheduled_event(
            GUILD,
            EVENT,
            status=True,  # type: ignore[arg-type]
        )
    bot.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduled_event_cover_uses_two_phase_authority_routes() -> None:
    bot = client()
    ticket = {
        "id": "90",
        "origin_domain": "chat.example",
        "filename": "event.png",
        "content_type": "image/png",
        "size": 5,
        "scan_status": "pending",
        "purpose": "scheduled_event_image",
        "upload_url": "https://media.chat.example/upload",
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            ticket,
            event_payload(image="a" * 64),
            event_payload(image=None),
        ]
    )
    bot._put_upload_ticket = AsyncMock()  # type: ignore[method-assign]

    updated = await bot.upload_scheduled_event_image(
        GUILD,
        EVENT,
        b"image",
        filename="event.png",
        content_type="image/png",
        target=TARGET,
        reason="event artwork",
    )
    cleared = await updated.delete_image(reason="clear artwork")

    assert updated.image_hash == "a" * 64
    assert cleared.image_hash is None
    assert bot.request.await_args_list[0].args[:2] == (
        "POST",
        "/api/v1/bots/guilds/10@chat.example/scheduled-events/20@chat.example/image/tickets",
    )
    assert bot.request.await_args_list[1].args[:2] == (
        "PUT",
        "/api/v1/bots/guilds/10@chat.example/scheduled-events/20@chat.example/image",
    )
    assert bot.request.await_args_list[2].args[:2] == (
        "DELETE",
        "/api/v1/bots/guilds/10@chat.example/scheduled-events/20@chat.example/image",
    )


@pytest.mark.asyncio
async def test_scheduled_event_cover_selects_qualified_authority_with_multiple_targets() -> (
    None
):
    bot = client()
    bot._targets.update(  # noqa: SLF001 - exercise target selection without network setup
        {
            "https://chat.example": AsyncMock(),
            "https://other.example": AsyncMock(),
        }
    )
    ticket = {
        "id": "90",
        "origin_domain": "chat.example",
        "filename": "event.png",
        "content_type": "image/png",
        "size": 5,
        "scan_status": "pending",
        "purpose": "scheduled_event_image",
        "upload_url": "https://media.chat.example/upload",
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[ticket, event_payload(image="a" * 64)]
    )
    bot._put_upload_ticket = AsyncMock()  # type: ignore[method-assign]

    await bot.upload_scheduled_event_image(
        GUILD,
        EVENT,
        b"image",
        filename="event.png",
        content_type="image/png",
    )

    assert all(
        call.kwargs["target"] == "https://chat.example"
        for call in bot.request.await_args_list
    )


@pytest.mark.asyncio
async def test_scheduled_event_delete_selects_qualified_authority_with_multiple_targets() -> (
    None
):
    bot = client()
    bot._targets.update(  # noqa: SLF001 - exercise target selection without network setup
        {
            "https://chat.example": AsyncMock(),
            "https://other.example": AsyncMock(),
        }
    )
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await bot.delete_scheduled_event(GUILD, EVENT)

    assert last_await(bot.request).kwargs["target"] == "https://chat.example"


@pytest.mark.asyncio
async def test_external_events_listing_fetch_and_user_pagination_are_typed() -> None:
    bot = client()
    external = event_payload(
        channel_id=None,
        channel_domain=None,
        entity_type=3,
        entity_metadata={"location": "Town Hall"},
        scheduled_end_time="2026-09-01T20:00:00+00:00",
    )
    user_item = {
        "guild_scheduled_event_id": "20",
        "guild_scheduled_event_domain": "chat.example",
        "user": {
            "id": "50",
            "origin_domain": "people.example",
            "username": "aya",
            "display_name": "Aya",
            "bot": False,
        },
        "member": None,
        "subscribed_at": "2026-08-28T00:00:00+00:00",
    }
    bot.request = AsyncMock(side_effect=[[external], external, [user_item]])  # type: ignore[method-assign]

    events = await bot.scheduled_events(GUILD, target=TARGET, with_user_count=True)
    fetched = await bot.fetch_scheduled_event(
        GUILD, EVENT, target=TARGET, with_user_count=True
    )
    users = await fetched.users(
        after=EntityRef(40, "people.example"), with_member=False
    )

    assert events[0].entity_metadata is not None
    assert events[0].entity_metadata.location == "Town Hall"
    assert fetched.entity_type == 3
    assert isinstance(users[0], ScheduledEventUser)
    assert users[0].user.ref == EntityRef(50, "people.example")
    assert last_await(bot.request).kwargs["params"] == {
        "limit": 100,
        "with_member": False,
        "after": "40@people.example",
    }


@pytest.mark.asyncio
async def test_scheduled_event_sdk_rejects_incomplete_entity_shapes() -> None:
    bot = client()
    start = datetime.now(UTC) + timedelta(hours=1)

    with pytest.raises(ValueError, match="stage and voice events require channel"):
        await bot.create_scheduled_event(
            GUILD,
            "Stage",
            start,
            entity_type=1,
        )
    with pytest.raises(ValueError, match="stage and voice events require channel"):
        await bot.create_scheduled_event(
            GUILD,
            "Voice",
            start,
            entity_type=2,
        )
    with pytest.raises(ValueError, match="external events require"):
        await bot.create_scheduled_event(
            GUILD,
            "External",
            start,
            entity_type=3,
            location="Town Hall",
        )


@pytest.mark.asyncio
async def test_scheduled_event_gateway_resource_and_subscription_events_are_typed() -> (
    None
):
    bot = client()
    seen: list[object] = []

    @bot.listen("GUILD_SCHEDULED_EVENT_CREATE")
    async def on_create(event: object) -> None:
        seen.append(event)

    @bot.listen("GUILD_SCHEDULED_EVENT_USER_ADD")
    async def on_subscribe(event: object) -> None:
        seen.append(event)

    await bot.dispatch("GUILD_SCHEDULED_EVENT_CREATE", event_payload(), target=TARGET)
    await bot.dispatch(
        "GUILD_SCHEDULED_EVENT_USER_ADD",
        {
            "guild_scheduled_event_id": "20",
            "guild_scheduled_event_domain": "chat.example",
            "guild_id": "10",
            "guild_domain": "chat.example",
            "user_id": "50",
            "user_domain": "people.example",
        },
        target=TARGET,
    )

    assert isinstance(seen[0], ScheduledEvent)
    assert isinstance(seen[1], ScheduledEventUserEvent)
    assert seen[1].added
