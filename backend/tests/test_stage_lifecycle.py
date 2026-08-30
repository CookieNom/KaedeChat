from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api.stage_instances import (
    StageInstanceCreate,
    StageInstancePatch,
    create_local_stage_instance,
    patch_local_stage_instance,
    update_local_stage_voice_state,
)
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import Channel, Guild, GuildMember, Message, MessageProjection, User
from app.voice.permissions import STAGE_INSTANCE_MODERATOR_PERMISSIONS
from app.voice.schemas import UserVoiceStateUpdate
from app.voice.stage_lifecycle import (
    STAGE_END_MESSAGE,
    STAGE_SPEAKER_MESSAGE,
    STAGE_START_MESSAGE,
    STAGE_TOPIC_MESSAGE,
    advance_stage_instance_lifecycle,
    persist_stage_system_message,
)
from app.voice.state import Occupant


def settings() -> Settings:
    return Settings(
        domain="alpha.localhost",
        environment="test",
        secret_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
        media_s3_access_key="GK00000000000000000000000000000000",
        media_s3_secret_key="0" * 64,
        voice_stage_empty_grace_seconds=120,
    )


def stage_channel() -> SimpleNamespace:
    return SimpleNamespace(
        id=30,
        origin_domain="alpha.localhost",
        guild_id=10,
        guild_domain="alpha.localhost",
        type=13,
        name="Town Hall",
        unavailable=False,
        encryption_policy_generation=0,
        encryption_epoch=None,
    )


def guild() -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        origin_domain="alpha.localhost",
        owner_id=1,
        owner_domain="alpha.localhost",
    )


def actor() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        origin_domain="alpha.localhost",
        username="aya",
        display_name="Aya",
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        account_type="human",
        disabled_at=None,
        profile_resolved=True,
        profile_version=1,
    )


@pytest.mark.asyncio
async def test_stage_system_message_is_durable_and_federated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added: list[object] = []

    class Session:
        def add(self, value: object) -> None:
            added.append(value)

        async def flush(self) -> None:
            for value in added:
                if isinstance(value, Message) and value.created_at is None:
                    value.created_at = datetime(2026, 8, 28, 12, tzinfo=UTC)

    queue_mutation = AsyncMock()
    queue_dispatch = Mock()
    monkeypatch.setattr(
        "app.voice.stage_lifecycle.message_payload",
        lambda message, _author, _attachments: {
            "id": str(message.id),
            "origin_domain": message.origin_domain,
            "channel_id": str(message.channel_id),
            "channel_domain": message.channel_domain,
            "content": message.content,
            "message_type": message.message_type,
        },
    )
    monkeypatch.setattr(
        "app.voice.stage_lifecycle.profile_from_user",
        lambda user: {"id": str(user.id), "origin_domain": user.origin_domain},
    )
    monkeypatch.setattr("app.voice.stage_lifecycle.queue_guild_mutation", queue_mutation)
    monkeypatch.setattr("app.voice.stage_lifecycle.queue_postcommit_dispatch", queue_dispatch)
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=90))
    channel = stage_channel()

    rendered = await persist_stage_system_message(
        cast(Any, Session()),
        settings(),
        cast(Any, snowflake),
        guild=cast(Any, guild()),
        channel=cast(Any, channel),
        author=cast(Any, actor()),
        message_type=STAGE_START_MESSAGE,
        topic="Town hall",
    )

    stored = next(value for value in added if isinstance(value, Message))
    projection = next(value for value in added if isinstance(value, MessageProjection))
    assert stored.message_type == 27
    assert stored.content == "Town hall"
    assert stored.author_id == 1
    assert projection.message_id == 90
    assert (channel.last_message_id, channel.last_message_domain) == (
        90,
        "alpha.localhost",
    )
    assert rendered["message_type"] == 27
    assert queue_mutation.await_args.args[4] == "guild.message.create"
    assert queue_dispatch.call_args.args[2] == "MESSAGE_CREATE"


@pytest.mark.asyncio
async def test_stage_create_emits_start_message_and_visible_notification_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = stage_channel()
    current_guild = guild()
    current_actor = actor()
    session = AsyncMock()
    session.add = Mock()
    session.scalar.side_effect = [channel, None]
    system_message = AsyncMock(return_value={"message_type": 27})
    mutation = AsyncMock()
    dispatch = Mock()
    monkeypatch.setattr(
        "app.api.stage_instances.stage_channel_and_guild",
        AsyncMock(return_value=(channel, current_guild)),
    )
    monkeypatch.setattr(
        "app.api.stage_instances.require_permissions",
        AsyncMock(
            return_value=(
                Permission.MANAGE_CHANNELS
                | Permission.MUTE_MEMBERS
                | Permission.MOVE_MEMBERS
                | Permission.MENTION_EVERYONE
            )
        ),
    )
    monkeypatch.setattr("app.api.stage_instances.add_audit_entry", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.queue_guild_mutation", mutation)
    monkeypatch.setattr("app.api.stage_instances.persist_stage_system_message", system_message)
    monkeypatch.setattr("app.api.stage_instances.queue_postcommit_dispatch", dispatch)
    monkeypatch.setattr("app.api.stage_instances.publish_committed_dispatches", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.wake_queued_guild_federation", AsyncMock())
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=60))

    rendered = await create_local_stage_instance(
        session,
        AsyncMock(),
        settings(),
        cast(Any, snowflake),
        cast(Any, current_actor),
        StageInstanceCreate(
            channel_id="30@alpha.localhost",
            topic="Town hall",
            send_start_notification=True,
        ),
        reason=None,
    )

    created = session.add.call_args.args[0]
    assert created.creator_id == current_actor.id
    assert created.creator_domain == current_actor.origin_domain
    assert rendered["topic"] == "Town hall"
    assert session.scalar.await_args_list[0].args[0]._for_update_arg is not None
    assert mutation.await_args.args[4] == "guild.stage.instance.create"
    assert mutation.await_args.args[5]["send_start_notification"] is True
    assert system_message.await_args.kwargs["message_type"] == STAGE_START_MESSAGE
    stage_dispatch = next(
        call for call in dispatch.call_args_list if call.args[2] == "STAGE_INSTANCE_CREATE"
    )
    assert stage_dispatch.args[3]["send_start_notification"] is True
    assert stage_dispatch.args[3]["notification_id"] == "60"
    assert stage_dispatch.args[3]["notification_author"] == {
        "id": "1",
        "origin_domain": "alpha.localhost",
    }


@pytest.mark.parametrize(
    ("event_guild_id", "event_channel_id"),
    [(11, 31), (10, 31)],
)
@pytest.mark.asyncio
async def test_stage_create_rejects_scheduled_event_outside_exact_channel_lineage(
    monkeypatch: pytest.MonkeyPatch,
    event_guild_id: int,
    event_channel_id: int,
) -> None:
    channel = stage_channel()
    current_guild = guild()
    session = AsyncMock()
    session.add = Mock()
    session.scalar.side_effect = [channel, None]
    session.get.return_value = SimpleNamespace(
        id=50,
        origin_domain="alpha.localhost",
        guild_id=event_guild_id,
        guild_domain="alpha.localhost",
        channel_id=event_channel_id,
        channel_domain="alpha.localhost",
        entity_type=1,
        status=1,
        entity_id=None,
    )
    monkeypatch.setattr(
        "app.api.stage_instances.stage_channel_and_guild",
        AsyncMock(return_value=(channel, current_guild)),
    )
    monkeypatch.setattr(
        "app.api.stage_instances.require_permissions",
        AsyncMock(return_value=STAGE_INSTANCE_MODERATOR_PERMISSIONS),
    )

    with pytest.raises(HTTPException) as invalid:
        await create_local_stage_instance(
            session,
            AsyncMock(),
            settings(),
            cast(Any, SimpleNamespace(mint=AsyncMock(return_value=60))),
            cast(Any, actor()),
            StageInstanceCreate(
                channel_id="30@alpha.localhost",
                topic="Town hall",
                guild_scheduled_event_id="50@alpha.localhost",
            ),
            reason=None,
        )

    assert invalid.value.status_code == 400
    assert invalid.value.detail == {"code": "STAGE_SCHEDULED_EVENT_INVALID"}
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_topic_change_emits_type_31_system_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = lifecycle_stage(empty_since=datetime.now(UTC))
    channel = stage_channel()
    current_guild = guild()
    current_actor = actor()
    session = AsyncMock()
    system_message = AsyncMock()
    monkeypatch.setattr(
        "app.api.stage_instances.local_stage_instance",
        AsyncMock(return_value=(instance, channel, current_guild)),
    )
    monkeypatch.setattr("app.api.stage_instances.require_permissions", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.add_audit_entry", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.queue_guild_mutation", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.persist_stage_system_message", system_message)
    monkeypatch.setattr("app.api.stage_instances.queue_postcommit_dispatch", Mock())
    monkeypatch.setattr("app.api.stage_instances.publish_committed_dispatches", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.wake_queued_guild_federation", AsyncMock())

    await patch_local_stage_instance(
        session,
        AsyncMock(),
        settings(),
        cast(Any, SimpleNamespace(mint=AsyncMock(return_value=92))),
        cast(Any, current_actor),
        EntityRef("30@alpha.localhost"),
        StageInstancePatch(topic="New topic"),
        reason=None,
    )

    assert system_message.await_args.kwargs["message_type"] == STAGE_TOPIC_MESSAGE
    assert system_message.await_args.kwargs["topic"] == "New topic"
    assert system_message.await_args.kwargs["author"] is current_actor


@pytest.mark.asyncio
async def test_stage_patch_noop_does_not_audit_or_emit_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = lifecycle_stage(empty_since=datetime.now(UTC))
    channel = stage_channel()
    current_guild = guild()
    current_actor = actor()
    session = AsyncMock()
    audit = AsyncMock()
    mutation = AsyncMock()
    dispatch = Mock()
    monkeypatch.setattr(
        "app.api.stage_instances.local_stage_instance",
        AsyncMock(return_value=(instance, channel, current_guild)),
    )
    monkeypatch.setattr("app.api.stage_instances.require_permissions", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.add_audit_entry", audit)
    monkeypatch.setattr("app.api.stage_instances.queue_guild_mutation", mutation)
    monkeypatch.setattr("app.api.stage_instances.queue_postcommit_dispatch", dispatch)

    rendered = await patch_local_stage_instance(
        session,
        AsyncMock(),
        settings(),
        cast(Any, SimpleNamespace(mint=AsyncMock(return_value=92))),
        cast(Any, current_actor),
        EntityRef("30@alpha.localhost"),
        StageInstancePatch(topic=instance.topic),
        reason="unchanged",
    )

    assert rendered["topic"] == instance.topic
    audit.assert_not_awaited()
    mutation.assert_not_awaited()
    dispatch.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_newly_unsuppressed_speaker_authors_type_29_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = Occupant(
        identity="40@people.example",
        user_id="40",
        user_domain="people.example",
        room="g.10.30",
        guild_id="10",
        channel_id="30",
        joined_at=1,
        suppressed=True,
        can_speak=False,
        can_stream=False,
    )
    promoted = replace(current, suppressed=False, can_speak=True)
    channel = stage_channel()
    current_guild = guild()
    moderator = actor()
    target = SimpleNamespace(
        id=40,
        origin_domain="people.example",
        account_type="human",
    )
    stage = lifecycle_stage(empty_since=datetime.now(UTC))
    session = AsyncMock()
    session.scalar.return_value = stage
    system_message = AsyncMock()
    monkeypatch.setattr(
        "app.api.stage_instances.stage_guild_for_actor",
        AsyncMock(return_value=current_guild),
    )
    monkeypatch.setattr(
        "app.api.stage_instances.connected_stage_occupant",
        AsyncMock(return_value=(current, channel, target)),
    )
    monkeypatch.setattr("app.api.stage_instances.require_permissions", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.require_can_manage_member", AsyncMock())
    monkeypatch.setattr(
        "app.api.stage_instances.get_permissions",
        AsyncMock(return_value=Permission(0)),
    )
    grant_update = AsyncMock(return_value=promoted)
    monkeypatch.setattr(
        "app.api.stage_instances.update_authoritative_occupant_grant",
        grant_update,
    )
    monkeypatch.setattr("app.api.stage_instances.persist_stage_system_message", system_message)
    monkeypatch.setattr("app.api.stage_instances.publish_committed_dispatches", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.publish_ephemeral", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.enqueue_best_effort", AsyncMock())

    await update_local_stage_voice_state(
        session,
        AsyncMock(),
        settings(),
        EntityRef("10@alpha.localhost"),
        cast(Any, moderator),
        EntityRef("40@people.example"),
        UserVoiceStateUpdate(
            channel_id=EntityRef("30@alpha.localhost"),
            suppress=False,
        ),
        current_user=False,
        snowflake=cast(Any, SimpleNamespace(mint=AsyncMock(return_value=93))),
    )

    assert system_message.await_args.kwargs["message_type"] == STAGE_SPEAKER_MESSAGE
    assert system_message.await_args.kwargs["author"] is target
    assert system_message.await_args.kwargs["topic"] == "Town hall"
    assert grant_update.await_args.kwargs["can_speak"] is True


def lifecycle_stage(*, empty_since: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=60,
        origin_domain="alpha.localhost",
        guild_id=10,
        guild_domain="alpha.localhost",
        channel_id=30,
        channel_domain="alpha.localhost",
        creator_id=1,
        creator_domain="alpha.localhost",
        topic="Town hall",
        privacy_level=2,
        discoverable_disabled=True,
        scheduled_event_id=None,
        scheduled_event_domain=None,
        empty_since=empty_since,
    )


def lifecycle_session(stage: SimpleNamespace) -> AsyncMock:
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[stage, None])
    channel = stage_channel()
    current_guild = guild()
    current_actor = actor()

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Channel:
            return channel
        if model is Guild:
            return current_guild
        if model is User:
            return current_actor
        if model is GuildMember:
            return SimpleNamespace()
        return None

    session.get = AsyncMock(side_effect=get)
    return session


@pytest.mark.asyncio
async def test_empty_stage_closes_after_grace_with_end_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 28, 12, 5, tzinfo=UTC)
    stage = lifecycle_stage(empty_since=now - timedelta(minutes=3))
    session = lifecycle_session(stage)
    occupancy = AsyncMock(side_effect=[[], [], []])
    persist = AsyncMock()
    mutation = AsyncMock()
    monkeypatch.setattr("app.voice.stage_lifecycle.room_occupants", occupancy)
    monkeypatch.setattr("app.voice.stage_lifecycle.persist_stage_system_message", persist)
    monkeypatch.setattr("app.voice.stage_lifecycle.queue_guild_mutation", mutation)
    monkeypatch.setattr("app.voice.stage_lifecycle.queue_postcommit_dispatch", Mock())
    monkeypatch.setattr("app.voice.stage_lifecycle.publish_committed_dispatches", AsyncMock())
    monkeypatch.setattr("app.voice.stage_lifecycle.wake_queued_guild_federation", AsyncMock())
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=91))

    closed = await advance_stage_instance_lifecycle(
        session,
        AsyncMock(),
        settings(),
        cast(Any, snowflake),
        now=now,
    )

    assert closed == 1
    assert persist.await_args.kwargs["message_type"] == STAGE_END_MESSAGE
    assert persist.await_args.kwargs["message_id"] == 91
    assert mutation.await_args.args[4] == "guild.stage.instance.delete"
    session.delete.assert_awaited_once_with(stage)
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_speaker_returning_at_close_edge_wins_the_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 28, 12, 5, tzinfo=UTC)
    stage = lifecycle_stage(empty_since=now - timedelta(minutes=3))
    session = lifecycle_session(stage)
    returning_speaker = SimpleNamespace(suppressed=False)
    monkeypatch.setattr(
        "app.voice.stage_lifecycle.room_occupants",
        AsyncMock(side_effect=[[], [returning_speaker]]),
    )
    persist = AsyncMock()
    monkeypatch.setattr("app.voice.stage_lifecycle.persist_stage_system_message", persist)
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=91))

    closed = await advance_stage_instance_lifecycle(
        session,
        AsyncMock(),
        settings(),
        cast(Any, snowflake),
        now=now,
    )

    assert closed == 0
    persist.assert_not_awaited()
    session.delete.assert_not_awaited()
    session.rollback.assert_awaited_once()
