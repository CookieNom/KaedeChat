from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

import app.api.scheduled_events as scheduled_events_api
from app.api.bots import bot_create_scheduled_event_image_ticket
from app.api.scheduled_events import (
    ACTIVE,
    CANCELED,
    COMPLETED,
    EXTERNAL,
    SCHEDULED,
    STAGE_INSTANCE,
    VALID_STATUS_TRANSITIONS,
    VOICE,
    ScheduledEventCreate,
    ScheduledEventPatch,
    ScheduledEventRecurrenceRule,
    _validate_entity_fields,
    active_scheduled_event_for_invite,
    scheduled_event_lifecycle_status,
    scheduled_event_payload,
    subscribe_scheduled_event,
)
from app.db.models import GuildScheduledEvent, GuildScheduledEventSubscription, Invite
from app.scheduled_events.recurrence import next_recurrence_start
from app.scheduled_events.service import (
    materialize_next_recurrence,
    scheduled_event_image_binding,
)


def event_model(**overrides: object) -> GuildScheduledEvent:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": 20,
        "origin_domain": "chat.example",
        "guild_id": 10,
        "guild_domain": "chat.example",
        "channel_id": 30,
        "channel_domain": "chat.example",
        "creator_id": 2,
        "creator_domain": "apps.example",
        "name": "Community call",
        "description": None,
        "scheduled_start_time": now + timedelta(hours=2),
        "scheduled_end_time": None,
        "privacy_level": 2,
        "status": SCHEDULED,
        "entity_type": VOICE,
        "entity_metadata": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return GuildScheduledEvent(**values)


def invite_model() -> Invite:
    now = datetime.now(UTC)
    return Invite(
        code="AbCd1234",
        guild_id=10,
        guild_domain="chat.example",
        inviter_id=2,
        inviter_domain="apps.example",
        target_type=None,
        scheduled_event_id=20,
        scheduled_event_domain="chat.example",
        created_at=now,
        updated_at=now,
    )


def test_discord_entity_field_contract_supports_stage_voice_and_external() -> None:
    now = datetime.now(UTC)
    channel = SimpleNamespace(id=30)

    _validate_entity_fields(
        entity_type=STAGE_INSTANCE,
        channel=channel,
        entity_metadata=None,
        scheduled_start_time=now,
        scheduled_end_time=None,
    )
    _validate_entity_fields(
        entity_type=VOICE,
        channel=channel,
        entity_metadata=None,
        scheduled_start_time=now,
        scheduled_end_time=None,
    )
    _validate_entity_fields(
        entity_type=EXTERNAL,
        channel=None,
        entity_metadata={"location": "Town Hall"},
        scheduled_start_time=now,
        scheduled_end_time=now + timedelta(hours=1),
    )

    with pytest.raises(HTTPException) as stage:
        _validate_entity_fields(
            entity_type=STAGE_INSTANCE,
            channel=None,
            entity_metadata=None,
            scheduled_start_time=now,
            scheduled_end_time=None,
        )
    assert stage.value.detail["code"] == "SCHEDULED_EVENT_ENTITY_FIELDS_INVALID"
    assert "voice and stage" in stage.value.detail["message"].lower()

    with pytest.raises(HTTPException) as external:
        _validate_entity_fields(
            entity_type=EXTERNAL,
            channel=None,
            entity_metadata={"location": "Town Hall"},
            scheduled_start_time=now,
            scheduled_end_time=None,
        )
    assert external.value.detail["code"] == "SCHEDULED_EVENT_END_TIME_REQUIRED"


def test_status_transitions_match_discord_terminal_semantics() -> None:
    assert {
        SCHEDULED: frozenset({ACTIVE, CANCELED}),
        ACTIVE: frozenset({COMPLETED}),
        COMPLETED: frozenset(),
        CANCELED: frozenset(),
    } == VALID_STATUS_TRANSITIONS


def test_automatic_lifecycle_matches_discord_event_types() -> None:
    now = datetime.now(UTC)

    assert (
        scheduled_event_lifecycle_status(
            event_model(
                entity_type=EXTERNAL,
                channel_id=None,
                channel_domain=None,
                entity_metadata={"location": "Town Hall"},
                scheduled_start_time=now - timedelta(minutes=1),
                scheduled_end_time=now + timedelta(hours=1),
            ),
            now=now,
        )
        == ACTIVE
    )
    assert (
        scheduled_event_lifecycle_status(
            event_model(
                entity_type=EXTERNAL,
                channel_id=None,
                channel_domain=None,
                entity_metadata={"location": "Town Hall"},
                status=ACTIVE,
                scheduled_start_time=now - timedelta(hours=2),
                scheduled_end_time=now - timedelta(minutes=1),
            ),
            now=now,
        )
        == COMPLETED
    )
    assert (
        scheduled_event_lifecycle_status(
            event_model(scheduled_start_time=now - timedelta(hours=3, minutes=1)),
            now=now,
        )
        == CANCELED
    )
    assert (
        scheduled_event_lifecycle_status(
            event_model(scheduled_start_time=now - timedelta(hours=2)),
            now=now,
        )
        is None
    )


def test_event_payload_uses_composite_refs_and_real_count() -> None:
    event = event_model()
    payload = scheduled_event_payload(event, user_count=7, me_subscribed=True)

    assert payload["id"] == "20"
    assert payload["origin_domain"] == "chat.example"
    assert payload["channel_id"] == "30"
    assert payload["channel_domain"] == "chat.example"
    assert payload["status"] == SCHEDULED
    assert payload["entity_type"] == VOICE
    assert payload["user_count"] == 7
    assert payload["me_subscribed"] is True


@pytest.mark.asyncio
async def test_image_update_materializes_version_before_render_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = datetime(2026, 8, 29, 12, tzinfo=UTC)
    refreshed_at = prior + timedelta(minutes=1)
    event = event_model(updated_at=prior, image_hash="a" * 64)
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    creator = SimpleNamespace(
        id=2,
        origin_domain="apps.example",
        username="events-bot",
        display_name=None,
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_resolved=True,
        account_type="bot",
    )
    lifecycle: list[str] = []

    async def flush() -> None:
        lifecycle.append("flush")
        event.updated_at = None  # type: ignore[assignment]

    async def refresh(
        value: object,
        *,
        attribute_names: tuple[str, ...],
    ) -> None:
        assert value is event
        assert attribute_names == ("updated_at",)
        event.updated_at = refreshed_at
        lifecycle.append("refresh")

    async def commit() -> None:
        lifecycle.append("commit")

    session = SimpleNamespace(
        flush=AsyncMock(side_effect=flush),
        refresh=AsyncMock(side_effect=refresh),
        commit=AsyncMock(side_effect=commit),
    )
    monkeypatch.setattr(scheduled_events_api, "_creator", AsyncMock(return_value=creator))
    monkeypatch.setattr(scheduled_events_api, "_subscriber_count", AsyncMock(return_value=3))
    monkeypatch.setattr(scheduled_events_api, "add_audit_entry", AsyncMock())
    monkeypatch.setattr(
        scheduled_events_api,
        "_queue_scheduled_event_projection",
        AsyncMock(),
    )
    monkeypatch.setattr(scheduled_events_api, "queue_postcommit_dispatch", Mock())
    monkeypatch.setattr(
        scheduled_events_api,
        "publish_committed_dispatches",
        AsyncMock(side_effect=lambda *_args: lifecycle.append("publish")),
    )
    monkeypatch.setattr(
        scheduled_events_api,
        "wake_queued_guild_federation",
        AsyncMock(),
    )

    rendered = await scheduled_events_api._publish_scheduled_event_image_change(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="chat.example")),
        cast(Any, guild),
        event,
        cast(Any, creator),
        old_hash=None,
        reason=None,
    )

    assert rendered["version"] == refreshed_at.isoformat()
    assert lifecycle == ["flush", "refresh", "commit", "publish"]


@pytest.mark.asyncio
async def test_terminal_or_wrong_guild_event_cannot_back_an_invite() -> None:
    invite = invite_model()
    session = SimpleNamespace(get=AsyncMock(return_value=event_model(status=ACTIVE)))
    assert await active_scheduled_event_for_invite(session, invite) is not None

    session.get.return_value = event_model(status=COMPLETED)
    assert await active_scheduled_event_for_invite(session, invite) is None

    session.get.return_value = event_model(guild_id=11)
    assert await active_scheduled_event_for_invite(session, invite) is None


def test_create_schema_preserves_discord_numeric_types_and_qualified_channel() -> None:
    payload = ScheduledEventCreate.model_validate(
        {
            "channel_id": "30@chat.example",
            "name": "Community call",
            "privacy_level": 2,
            "scheduled_start_time": "2026-09-01T18:00:00+00:00",
            "entity_type": 2,
        }
    )

    assert str(payload.channel_id) == "30@chat.example"
    assert payload.entity_type == VOICE

    stage = ScheduledEventCreate.model_validate(
        {
            "channel_id": "31@chat.example",
            "name": "Stage town hall",
            "privacy_level": 2,
            "scheduled_start_time": "2026-09-01T18:00:00+00:00",
            "entity_type": 1,
        }
    )
    assert str(stage.channel_id) == "31@chat.example"
    assert stage.entity_type == STAGE_INSTANCE

    with pytest.raises(ValueError, match="entity_type must be an integer"):
        ScheduledEventCreate.model_validate(
            {
                "channel_id": "31@chat.example",
                "name": "Invalid boolean event",
                "privacy_level": 2,
                "scheduled_start_time": "2026-09-01T18:00:00+00:00",
                "entity_type": True,
            }
        )
    with pytest.raises(ValueError, match="status must be an integer"):
        ScheduledEventPatch.model_validate({"status": True})


@pytest.mark.asyncio
async def test_subscribe_locks_event_before_terminal_state_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    event = event_model(status=COMPLETED)
    actor = SimpleNamespace(id=2, origin_domain="users.example")
    session = SimpleNamespace()
    lookup = AsyncMock(return_value=event)
    monkeypatch.setattr(
        "app.api.scheduled_events._proxy_human",
        AsyncMock(return_value=(False, None)),
    )
    monkeypatch.setattr(
        "app.api.scheduled_events.local_guild",
        AsyncMock(return_value=guild),
    )
    monkeypatch.setattr(
        "app.api.scheduled_events.scheduled_event_for_guild",
        lookup,
    )
    monkeypatch.setattr(
        "app.api.scheduled_events._require_event_view",
        AsyncMock(),
    )

    with pytest.raises(HTTPException) as terminal:
        await subscribe_scheduled_event(
            guild_ref=SimpleNamespace(),
            event_ref=SimpleNamespace(),
            auth=SimpleNamespace(user=actor),
            session=session,
            redis=SimpleNamespace(),
            settings=SimpleNamespace(),
        )

    assert terminal.value.detail["code"] == "SCHEDULED_EVENT_TERMINAL"
    assert lookup.await_args.kwargs == {"for_update": True}


@pytest.mark.asyncio
async def test_bot_event_cover_ticket_requires_attachment_write_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    installation = SimpleNamespace()
    principal = SimpleNamespace()
    require_scope = Mock(
        side_effect=HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": "attachments.write"},
        )
    )
    monkeypatch.setattr(
        "app.api.bots.installation_for_guild",
        AsyncMock(return_value=(guild, installation)),
    )
    monkeypatch.setattr("app.api.bots.require_installation_scope", require_scope)
    event_lookup = AsyncMock()
    monkeypatch.setattr("app.api.bots.scheduled_event_for_guild", event_lookup)

    with pytest.raises(HTTPException) as denied:
        await bot_create_scheduled_event_image_ticket(
            guild_ref=SimpleNamespace(),
            event_ref=SimpleNamespace(),
            payload=SimpleNamespace(),
            response=SimpleNamespace(),
            principal=principal,
            session=SimpleNamespace(),
            redis=SimpleNamespace(),
            snowflake=SimpleNamespace(),
            settings=SimpleNamespace(),
        )

    assert denied.value.detail == {
        "code": "BOT_SCOPE_REQUIRED",
        "scope": "attachments.write",
    }
    require_scope.assert_called_once_with(principal, installation, "attachments.write")
    event_lookup.assert_not_awaited()


def test_recurrence_frequency_uses_discord_yearly_through_daily_order() -> None:
    start = datetime(2026, 9, 1, 18, tzinfo=UTC)
    daily = ScheduledEventRecurrenceRule(
        start=start,
        frequency=3,
        by_weekday=[0, 1, 2, 3, 4],
    )
    weekly = ScheduledEventRecurrenceRule(
        start=start,
        frequency=2,
        interval=2,
        by_weekday=[1],
    )
    monthly = ScheduledEventRecurrenceRule(
        start=start,
        frequency=1,
        by_n_weekday=[{"n": 1, "day": 1}],
    )
    yearly = ScheduledEventRecurrenceRule(
        start=start,
        frequency=0,
        by_month=[9],
        by_month_day=[1],
    )

    assert [daily.frequency, weekly.frequency, monthly.frequency, yearly.frequency] == [3, 2, 1, 0]
    with pytest.raises(ValueError, match="only weekly"):
        ScheduledEventRecurrenceRule(start=start, frequency=1, interval=2)
    with pytest.raises(ValueError, match="monthly"):
        ScheduledEventRecurrenceRule(
            start=start,
            frequency=2,
            by_n_weekday=[{"n": 1, "day": 1}],
        )
    with pytest.raises(ValueError, match="frequency must be an integer"):
        ScheduledEventRecurrenceRule(start=start, frequency=True)
    with pytest.raises(ValueError, match="interval must be an integer"):
        ScheduledEventRecurrenceRule(start=start, frequency=2, interval=True)
    with pytest.raises(ValueError, match="by_weekday must be an integer"):
        ScheduledEventRecurrenceRule(start=start, frequency=2, by_weekday=[False])
    with pytest.raises(ValueError, match="n must be an integer"):
        ScheduledEventRecurrenceRule(
            start=start,
            frequency=1,
            by_n_weekday=[{"n": True, "day": 1}],
        )


def test_recurrence_calculator_handles_discord_writable_selectors() -> None:
    friday = datetime(2026, 8, 28, 18, tzinfo=UTC)
    weekday_rule = ScheduledEventRecurrenceRule(
        start=friday,
        frequency=3,
        by_weekday=[0, 1, 2, 3, 4],
    ).model_dump(mode="json")
    assert next_recurrence_start(weekday_rule, current_start=friday) == datetime(
        2026, 8, 31, 18, tzinfo=UTC
    )

    biweekly_rule = ScheduledEventRecurrenceRule(
        start=datetime(2026, 9, 2, 18, tzinfo=UTC),
        frequency=2,
        interval=2,
        by_weekday=[2],
    ).model_dump(mode="json")
    assert next_recurrence_start(
        biweekly_rule,
        current_start=datetime(2026, 9, 2, 18, tzinfo=UTC),
    ) == datetime(2026, 9, 16, 18, tzinfo=UTC)

    fifth_monday = ScheduledEventRecurrenceRule(
        start=datetime(2026, 8, 31, 18, tzinfo=UTC),
        frequency=1,
        by_n_weekday=[{"n": 5, "day": 0}],
    ).model_dump(mode="json")
    assert next_recurrence_start(
        fifth_monday,
        current_start=datetime(2026, 8, 31, 18, tzinfo=UTC),
    ) == datetime(2026, 11, 30, 18, tzinfo=UTC)

    leap_day = ScheduledEventRecurrenceRule(
        start=datetime(2024, 2, 29, 18, tzinfo=UTC),
        frequency=0,
        by_month=[2],
        by_month_day=[29],
    ).model_dump(mode="json")
    assert next_recurrence_start(
        leap_day,
        current_start=datetime(2024, 2, 29, 18, tzinfo=UTC),
    ) == datetime(2028, 2, 29, 18, tzinfo=UTC)


@pytest.mark.asyncio
async def test_terminal_recurrence_materializes_next_occurrence_with_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 8, 28, 18, tzinfo=UTC)
    event = event_model(
        status=COMPLETED,
        scheduled_start_time=start,
        scheduled_end_time=start + timedelta(hours=2),
        image_hash="a" * 64,
        recurrence_rule=ScheduledEventRecurrenceRule(
            start=start,
            frequency=3,
        ).model_dump(mode="json"),
    )
    subscription = GuildScheduledEventSubscription(
        event_id=event.id,
        event_domain=event.origin_domain,
        guild_id=event.guild_id,
        guild_domain=event.guild_domain,
        user_id=40,
        user_domain="people.example",
    )
    added: list[object] = []
    image = SimpleNamespace(asset_binding=scheduled_event_image_binding(event))

    class Session:
        async def scalars(self, _statement: object) -> list[GuildScheduledEventSubscription]:
            return [subscription]

        async def scalar(self, _statement: object) -> object:
            return image

        def add(self, value: object) -> None:
            added.append(value)

        async def flush(self) -> None:
            for value in added:
                if isinstance(value, GuildScheduledEvent):
                    value.created_at = start
                    value.updated_at = start

    mutation = AsyncMock()
    dispatch = Mock()
    monkeypatch.setattr("app.scheduled_events.service.queue_guild_mutation", mutation)
    monkeypatch.setattr("app.scheduled_events.service.queue_postcommit_dispatch", dispatch)
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=99))
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    creator = SimpleNamespace(
        id=2,
        origin_domain="apps.example",
        username="scheduler",
        display_name=None,
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        account_type="human",
        disabled_at=None,
        profile_resolved=True,
        profile_version=1,
    )

    next_event = await materialize_next_recurrence(
        Session(),  # type: ignore[arg-type]
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        snowflake,  # type: ignore[arg-type]
        guild=guild,  # type: ignore[arg-type]
        event=event,
        creator=creator,  # type: ignore[arg-type]
        channel=SimpleNamespace(),  # type: ignore[arg-type]
        after=start + timedelta(hours=3),
    )

    assert next_event is not None
    assert next_event.id == 99
    assert next_event.status == SCHEDULED
    assert next_event.scheduled_start_time == start + timedelta(days=1)
    assert next_event.scheduled_end_time == start + timedelta(days=1, hours=2)
    assert image.asset_binding == scheduled_event_image_binding(next_event)
    copied = next(item for item in added if isinstance(item, GuildScheduledEventSubscription))
    assert (copied.user_id, copied.user_domain) == (40, "people.example")
    assert mutation.await_args.args[4] == "guild.scheduled_event.create"
    assert dispatch.call_args.args[2] == "GUILD_SCHEDULED_EVENT_CREATE"
