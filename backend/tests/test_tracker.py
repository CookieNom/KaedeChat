from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api import guild_management_federation as guild_management_api
from app.api import tracker as tracker_api
from app.api.bot_gateway import event_intent, event_scope
from app.api.dependencies import AuthenticatedUser
from app.auth.tokens import AccessGrant
from app.chat.schemas import ChannelCreate
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import (
    Channel,
    Guild,
    TrackerBoard,
    TrackerDispatchOutbox,
    TrackerLane,
    TrackerTask,
    User,
)
from app.federation.guild_management import GuildManagementRequest, GuildManagementResult
from app.federation.guilds import SNAPSHOT_NEUTRAL_GUILD_EVENTS
from app.tracker import outbox as tracker_outbox
from app.tracker import service as tracker_service
from app.tracker.membership import clear_tracker_assignees
from app.tracker.payloads import tracker_board_payload
from app.tracker.schemas import (
    TrackerBoardUpdate,
    TrackerLaneUpdate,
    TrackerTaskCreate,
    TrackerTaskMove,
    TrackerTaskUpdate,
)
from app.tracker.service import (
    DEFAULT_TRACKER_LANES,
    TrackerContext,
    create_tracker_state,
    default_key_prefix,
    next_tracker_version,
    other_positions_need_normalization,
    require_tracker_version,
    task_request_fingerprint,
)


def settings(domain: str = "home.example") -> Settings:
    return Settings(
        domain=domain,
        environment="test",
        secret_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
        media_s3_access_key="GK00000000000000000000000000000000",
        media_s3_secret_key="0" * 64,
    )


def test_tracker_invalidations_do_not_advance_structural_snapshot_generation() -> None:
    assert "guild.tracker.board.invalidate" in SNAPSHOT_NEUTRAL_GUILD_EVENTS


def test_tracker_versions_are_strictly_monotonic_when_clocks_collide_or_regress() -> None:
    current = datetime(2026, 8, 26, 12, 0, 0, 123456, tzinfo=UTC)

    assert next_tracker_version(current, now=current) == current.replace(microsecond=123457)
    assert next_tracker_version(current, now=current - timedelta(seconds=1)) == current.replace(
        microsecond=123457
    )
    assert next_tracker_version(current, now=current + timedelta(seconds=1)) == current + timedelta(
        seconds=1
    )


def user(*, user_id: int = 8, domain: str = "home.example") -> User:
    return User(
        id=user_id,
        origin_domain=domain,
        is_local=domain == "home.example",
        username=f"user-{user_id}",
        account_type="human",
        profile_resolved=True,
        profile_version=1,
        e2ee_device_generation=0,
    )


def auth(actor: User) -> AuthenticatedUser:
    return AuthenticatedUser(
        actor,
        AccessGrant(actor.id, actor.origin_domain, "session"),
        "",
        False,
    )


def test_tracker_channel_schema_and_prefixes_are_strict() -> None:
    created = ChannelCreate(type=17, name="Raid Planning", tracker_key_prefix=" rp7 ")
    assert created.tracker_key_prefix == "RP7"
    assert default_key_prefix("Raid Planning", 42) == "RP"
    assert default_key_prefix("7", 12345678) == "KT12345678"

    with pytest.raises(ValidationError, match="only valid for tracker channels"):
        ChannelCreate(type=0, name="general", tracker_key_prefix="GEN")
    with pytest.raises(ValidationError, match="start with a letter"):
        ChannelCreate(type=17, name="tasks", tracker_key_prefix="7OPS")


def test_tracker_task_schema_requires_aware_dates_and_semantic_updates() -> None:
    with pytest.raises(ValidationError, match="timezone offset"):
        TrackerTaskCreate(
            lane_id="7@home.example",
            title="Ship it",
            due_at=datetime(2026, 8, 30),
        )
    with pytest.raises(ValidationError, match="at least one task field"):
        TrackerTaskUpdate()

    payload = TrackerTaskCreate(
        lane_id="7@home.example",
        title="  Ship it  ",
        due_at=datetime(2026, 8, 30, tzinfo=UTC),
        client_nonce="deploy:42",
    )
    assert payload.title == "Ship it"
    assert task_request_fingerprint(payload) == task_request_fingerprint(payload.model_copy())
    assert len(task_request_fingerprint(payload)) == 64


def test_tracker_dispatch_is_queued_in_the_mutation_transaction() -> None:
    added: list[object] = []
    session = SimpleNamespace(add=added.append)
    row = tracker_outbox.queue_tracker_dispatch(
        cast(Any, session),
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        event_type="TRACKER_TASK_UPDATE",
        payload={"task_id": "52", "version": "v2"},
    )
    assert added == [row]
    assert row.payload["version"] == "v2"

    with pytest.raises(ValueError, match="unsupported tracker dispatch"):
        tracker_outbox.queue_tracker_dispatch(
            cast(Any, session),
            channel_id=50,
            channel_domain="home.example",
            guild_id=10,
            guild_domain="home.example",
            event_type="MESSAGE_CREATE",
            payload={},
        )


@pytest.mark.asyncio
async def test_tracker_outbox_retains_failures_and_acknowledges_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    row = TrackerDispatchOutbox(
        id=7,
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        event_type="TRACKER_BOARD_UPDATE",
        payload={"channel_id": "50"},
        attempts=0,
        next_attempt_at=now,
        created_at=now,
    )
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[row]),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    publish = AsyncMock(return_value=None)
    monkeypatch.setattr(tracker_outbox, "publish_dispatch", publish)

    delivered = await tracker_outbox.drain_tracker_dispatch_outbox(
        cast(Any, session), cast(Any, SimpleNamespace())
    )
    assert delivered == 0
    assert row.attempts == 1
    assert row.next_attempt_at > now
    session.delete.assert_not_awaited()
    session.commit.assert_awaited_once()

    session.commit.reset_mock()
    publish.return_value = {"topic_seq": 1}
    row.next_attempt_at = datetime.now(UTC)
    delivered = await tracker_outbox.drain_tracker_dispatch_outbox(
        cast(Any, session), cast(Any, SimpleNamespace())
    )
    assert delivered == 1
    session.delete.assert_awaited_once_with(row)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_tracker_outbox_replays_after_publish_before_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    row = TrackerDispatchOutbox(
        id=9,
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        event_type="TRACKER_TASK_UPDATE",
        payload={"task_id": "52", "version": "v3"},
        attempts=0,
        next_attempt_at=now,
        created_at=now,
    )
    publish = AsyncMock(return_value={"topic_seq": 1})
    monkeypatch.setattr(tracker_outbox, "publish_dispatch", publish)
    interrupted = SimpleNamespace(
        scalars=AsyncMock(return_value=[row]),
        delete=AsyncMock(),
        commit=AsyncMock(side_effect=RuntimeError("worker stopped before ack")),
    )
    with pytest.raises(RuntimeError, match="before ack"):
        await tracker_outbox.drain_tracker_dispatch_outbox(
            cast(Any, interrupted), cast(Any, SimpleNamespace())
        )

    resumed = SimpleNamespace(
        scalars=AsyncMock(return_value=[row]),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    assert (
        await tracker_outbox.drain_tracker_dispatch_outbox(
            cast(Any, resumed), cast(Any, SimpleNamespace())
        )
        == 1
    )
    assert publish.await_count == 2


@pytest.mark.asyncio
async def test_tracker_mutation_queues_dispatch_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    board = TrackerBoard(
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        key_prefix="OLD",
        next_task_number=1,
        created_at=now,
        updated_at=now,
    )
    context = TrackerContext(
        access=cast(
            Any,
            SimpleNamespace(guild=SimpleNamespace(id=10, origin_domain="home.example")),
        ),
        board=board,
        permissions=int(Permission.MANAGE_TRACKER),
    )
    order: list[str] = []

    async def commit() -> None:
        order.append("commit")

    session = SimpleNamespace(commit=commit, refresh=AsyncMock())
    monkeypatch.setattr(tracker_service, "tracker_context", AsyncMock(return_value=context))
    monkeypatch.setattr(tracker_service, "board_response", AsyncMock(return_value={}))
    monkeypatch.setattr(
        tracker_service,
        "queue_context_dispatch",
        lambda *_args, **_kwargs: order.append("queue"),
    )
    monkeypatch.setattr(tracker_service, "queue_context_federation", AsyncMock())
    monkeypatch.setattr(tracker_service, "wake_context_outboxes", AsyncMock())

    await tracker_service.update_board(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        settings(),
        auth(user()),
        EntityRef("50"),
        TrackerBoardUpdate(key_prefix="NEW"),
        now.isoformat(),
    )

    assert order == ["queue", "commit"]


@pytest.mark.asyncio
async def test_tracker_creation_materializes_a_normalized_default_board() -> None:
    channel = Channel(
        id=50,
        origin_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        type=17,
        name="Raid Planning",
        created_floor_id=50,
    )
    added: list[object] = []
    session = SimpleNamespace(add=added.append)
    snowflake = SimpleNamespace(mint=AsyncMock(side_effect=[51, 52, 53, 54]))

    board = await create_tracker_state(
        cast(Any, session),
        cast(Any, snowflake),
        channel,
    )

    assert board in added
    assert board.key_prefix == "RP"
    assert board.next_task_number == 1
    lanes = [item for item in added if isinstance(item, TrackerLane)]
    assert [(lane.name, lane.position, lane.completed) for lane in lanes] == [
        (name, position, completed)
        for position, (name, _color, _kind, completed) in enumerate(DEFAULT_TRACKER_LANES)
    ]


def test_tracker_payload_has_stable_keys_order_counts_and_versions() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    board = TrackerBoard(
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        key_prefix="RAID",
        next_task_number=2,
        created_at=now,
        updated_at=now,
    )
    lane = TrackerLane(
        id=51,
        origin_domain="home.example",
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        name="Planned",
        color=0xF59E0B,
        kind="planned",
        completed=False,
        position=0,
        created_at=now,
        updated_at=now,
    )
    creator = user()
    task = TrackerTask(
        id=52,
        origin_domain="home.example",
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        lane_id=51,
        lane_domain="home.example",
        number=1,
        title="Publish strategy",
        priority="high",
        position=0,
        creator_id=creator.id,
        creator_domain=creator.origin_domain,
        created_at=now,
        updated_at=now,
    )

    rendered = tracker_board_payload(
        board,
        [lane],
        [task],
        {(creator.id, creator.origin_domain): creator},
        permissions=int(Permission.VIEW_CHANNEL | Permission.CREATE_TRACKER_TASKS),
    )

    assert rendered["version"] == now.isoformat()
    assert rendered["lanes"][0]["task_count"] == 1  # type: ignore[index]
    assert rendered["tasks"][0]["key"] == "RAID-1"  # type: ignore[index]
    assert rendered["tasks"][0]["creator"]["id"] == "8"  # type: ignore[index]


def test_tracker_if_match_is_required_and_conflict_safe() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    require_tracker_version(now, now.isoformat())
    require_tracker_version(now, f'"{now.isoformat()}"')

    with pytest.raises(HTTPException) as missing:
        require_tracker_version(now, None)
    assert missing.value.status_code == 428
    assert missing.value.detail["code"] == "TRACKER_VERSION_REQUIRED"

    with pytest.raises(HTTPException) as stale:
        require_tracker_version(now, "stale")
    assert stale.value.status_code == 412
    assert stale.value.detail["code"] == "TRACKER_VERSION_CONFLICT"


def test_position_invalidation_only_fires_when_other_resources_shift() -> None:
    first = SimpleNamespace(position=0, updated_at=datetime.now(UTC))
    target = SimpleNamespace(position=1, updated_at=datetime.now(UTC))
    assert not other_positions_need_normalization([first, target], target=target)

    shifted = SimpleNamespace(position=1, updated_at=datetime.now(UTC))
    assert other_positions_need_normalization([first, target, shifted], target=target)


@pytest.mark.asyncio
async def test_member_removal_clears_assignments_and_versions_the_whole_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Guild",
        owner_id=8,
        owner_domain="home.example",
    )
    board = TrackerBoard(
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        key_prefix="RAID",
        next_task_number=2,
        created_at=now,
        updated_at=now,
    )
    channel = Channel(
        id=50,
        origin_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        type=17,
        name="Tracker",
        created_floor_id=50,
    )

    class Rows:
        def __init__(self, rows: list[tuple[Any, ...]]) -> None:
            self.rows = rows

        def tuples(self) -> list[tuple[Any, ...]]:
            return self.rows

    execute = AsyncMock(
        side_effect=[
            Rows([(50, "home.example")]),
            Rows([(board, channel)]),
            SimpleNamespace(),
        ]
    )
    added: list[object] = []
    session = SimpleNamespace(execute=execute, add=added.append)
    queue_federation = AsyncMock()
    monkeypatch.setattr(
        "app.tracker.membership.queue_tracker_federation_invalidation",
        queue_federation,
    )

    refreshes = await clear_tracker_assignees(
        cast(Any, session),
        settings(),
        guild,
        user(),
        [(9, "remote.example"), (9, "remote.example")],
    )

    assert len(refreshes) == 1
    assert board.updated_at > now
    assert refreshes[0].version == board.updated_at
    candidate_sql = str(
        execute.await_args_list[0]
        .args[0]
        .compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    update_sql = str(
        execute.await_args_list[2]
        .args[0]
        .compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "tracker_tasks.assignee_id" in candidate_sql
    assert "UPDATE tracker_tasks SET assignee_id=NULL, assignee_domain=NULL" in update_sql
    assert "updated_at=" in update_sql
    queue_federation.assert_awaited_once()
    assert queue_federation.await_args.kwargs == {"reason": "assignee_membership_removed"}
    queued = next(item for item in added if isinstance(item, TrackerDispatchOutbox))
    assert queued.event_type == "TRACKER_BOARD_UPDATE"
    assert queued.payload["full_refresh"] is True
    assert queued.payload["reason"] == "assignee_membership_removed"
    assert queued.payload["version"] == board.updated_at.isoformat()


@pytest.mark.asyncio
async def test_tracker_context_keeps_a_local_authority_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = user()
    access = SimpleNamespace(
        channel=SimpleNamespace(type=17),
        guild=SimpleNamespace(origin_domain="remote.example"),
    )
    monkeypatch.setattr(
        tracker_service,
        "load_channel_access",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(
        tracker_service,
        "lock_local_channel_mutation",
        AsyncMock(return_value=access),
    )

    with pytest.raises(HTTPException) as rejected:
        await tracker_service.tracker_context(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            settings(),
            auth(actor),
            EntityRef("50@remote.example"),
            mutation=True,
            needed=Permission.VIEW_CHANNEL,
        )
    assert rejected.value.status_code == 409
    assert rejected.value.detail == {
        "code": "TRACKER_AUTHORITY_REQUIRED",
        "authority_domain": "remote.example",
    }


@pytest.mark.asyncio
async def test_remote_human_tracker_mutation_is_proxied_to_guild_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = user()
    access = SimpleNamespace(
        channel=SimpleNamespace(id=50, origin_domain="remote.example", type=17),
        guild=SimpleNamespace(id=10, origin_domain="remote.example"),
    )
    proxy = AsyncMock(
        return_value=GuildManagementResult(
            request_id="kagm_" + "a" * 32,
            operation="tracker.board.update",
            guild={"id": "10", "domain": "remote.example"},
            status_code=200,
            body={"key_prefix": "OPS"},
        )
    )
    monkeypatch.setattr(tracker_service, "load_channel_access", AsyncMock(return_value=access))
    monkeypatch.setattr(tracker_service, "proxy_remote_guild_management", proxy)
    local_context = AsyncMock()
    monkeypatch.setattr(tracker_service, "tracker_context", local_context)

    rendered = await tracker_service.update_board(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        settings(),
        auth(actor),
        EntityRef("50@remote.example"),
        TrackerBoardUpdate(key_prefix="OPS"),
        '"version"',
    )

    assert rendered == {"key_prefix": "OPS"}
    local_context.assert_not_awaited()
    assert proxy.await_args.args[2:] == (
        EntityRef("10@remote.example"),
        actor,
        "tracker.board.update",
        {
            "channel_ref": "50@remote.example",
            "data": {"key_prefix": "OPS"},
            "if_match": '"version"',
        },
    )


@pytest.mark.asyncio
async def test_remote_bot_tracker_mutation_requires_direct_authority_target() -> None:
    actor = user(domain="apps.example")
    actor.account_type = "bot"

    with pytest.raises(HTTPException) as rejected:
        await tracker_service.proxy_remote_tracker_mutation(
            cast(Any, SimpleNamespace()),
            settings(),
            auth(actor),
            EntityRef("50@remote.example"),
            "tracker.board.update",
            {"data": {"key_prefix": "OPS"}},
        )

    assert rejected.value.status_code == 409
    assert rejected.value.detail == {
        "code": "BOT_RESOURCE_AUTHORITY_REQUIRED",
        "resource_ref": "50@remote.example",
        "authority_domain": "remote.example",
    }


@pytest.mark.asyncio
async def test_tracker_authority_dispatch_validates_and_reuses_local_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = user(domain="remote.example")
    create = AsyncMock(return_value={"id": "70", "origin_domain": "home.example"})
    monkeypatch.setattr(tracker_service, "create_lane", create)
    request = GuildManagementRequest.model_validate(
        {
            "guild": {"id": "10", "domain": "home.example"},
            "actor": {"id": str(actor.id), "domain": actor.origin_domain},
            "requesting_instance": actor.origin_domain,
            "request_id": "kagm_" + "a" * 32,
            "issued_at": 10,
            "deadline": 20,
            "operation": "tracker.lane.create",
            "payload": {
                "channel_ref": "50@home.example",
                "data": {"name": " Ready ", "color": 42},
            },
        }
    )

    result = await guild_management_api._dispatch_tracker(
        request,
        actor,
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        settings(),
    )

    assert result.status_code == 201
    assert result.body == {"id": "70", "origin_domain": "home.example"}
    assert create.await_args.args[5] == EntityRef("50@home.example")
    assert create.await_args.args[6].name == "Ready"
    assert create.await_args.args[6].color == 42


@pytest.mark.asyncio
async def test_completed_lane_transition_emits_a_single_full_board_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    actor = user()
    board = TrackerBoard(
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        key_prefix="RAID",
        next_task_number=2,
        created_at=now,
        updated_at=now,
    )
    lane = TrackerLane(
        id=51,
        origin_domain="home.example",
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        name="Review",
        color=0,
        kind="custom",
        completed=False,
        position=0,
        created_at=now,
        updated_at=now,
    )
    task = TrackerTask(
        id=52,
        origin_domain="home.example",
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        lane_id=51,
        lane_domain="home.example",
        number=1,
        title="Review release",
        priority="none",
        position=0,
        creator_id=actor.id,
        creator_domain=actor.origin_domain,
        created_at=now,
        updated_at=now,
    )
    context = TrackerContext(
        access=cast(Any, SimpleNamespace(guild=SimpleNamespace(), channel=SimpleNamespace())),
        board=board,
        permissions=int(Permission.MANAGE_TRACKER),
    )
    session = SimpleNamespace(
        add=lambda _row: None,
        commit=AsyncMock(),
        refresh=AsyncMock(),
        scalar=AsyncMock(return_value=1),
    )
    queued = []
    monkeypatch.setattr(tracker_service, "tracker_context", AsyncMock(return_value=context))
    monkeypatch.setattr(tracker_service, "lane_by_ref", AsyncMock(return_value=lane))
    monkeypatch.setattr(tracker_service, "ordered_tasks", AsyncMock(return_value=[task]))
    monkeypatch.setattr(
        tracker_service,
        "queue_context_dispatch",
        lambda _session, _context, event_type, payload: queued.append((event_type, payload)),
    )
    monkeypatch.setattr(tracker_service, "queue_context_federation", AsyncMock())
    monkeypatch.setattr(tracker_service, "wake_context_outboxes", AsyncMock())

    rendered = await tracker_service.update_lane(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        settings(),
        auth(actor),
        EntityRef("50"),
        EntityRef("51"),
        TrackerLaneUpdate(completed=True),
        now.isoformat(),
    )

    assert rendered["completed"] is True
    assert task.completed_at is not None
    assert [event_type for event_type, _payload in queued] == ["TRACKER_BOARD_UPDATE"]
    assert queued[0][1]["reason"] == "lane_completion_updated"


@pytest.mark.asyncio
async def test_board_invalidation_is_an_explicit_refetch_contract() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    board = TrackerBoard(
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        key_prefix="RAID",
        next_task_number=2,
        created_at=now,
        updated_at=now,
    )
    context = TrackerContext(
        access=cast(
            Any,
            SimpleNamespace(guild=SimpleNamespace(id=10, origin_domain="home.example")),
        ),
        board=board,
        permissions=int(Permission.VIEW_CHANNEL),
    )
    added: list[TrackerDispatchOutbox] = []
    tracker_service.queue_board_update(
        cast(Any, SimpleNamespace(add=added.append)), context, reason="lane_completion_updated"
    )
    event = added[0].payload
    assert event["full_refresh"] is True
    assert event["reason"] == "lane_completion_updated"
    assert event["version"] == now.isoformat()


@pytest.mark.asyncio
async def test_task_create_nonce_replays_same_fingerprint_and_rejects_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    actor = user()
    board = TrackerBoard(
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        key_prefix="RAID",
        next_task_number=2,
        created_at=now,
        updated_at=now,
    )
    payload = TrackerTaskCreate(
        lane_id="51",
        title="Idempotent task",
        priority="high",
        client_nonce="client:request-7",
    )
    existing = TrackerTask(
        id=52,
        origin_domain="home.example",
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        lane_id=51,
        lane_domain="home.example",
        number=1,
        title=payload.title,
        priority=payload.priority,
        position=0,
        creator_id=actor.id,
        creator_domain=actor.origin_domain,
        client_nonce=payload.client_nonce,
        client_request_hash=task_request_fingerprint(payload),
        created_at=now,
        updated_at=now,
    )
    context = TrackerContext(
        access=cast(Any, SimpleNamespace()),
        board=board,
        permissions=int(Permission.CREATE_TRACKER_TASKS),
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=existing))
    snowflake = SimpleNamespace(mint=AsyncMock())
    monkeypatch.setattr(tracker_service, "tracker_context", AsyncMock(return_value=context))
    monkeypatch.setattr(
        tracker_service,
        "task_users",
        AsyncMock(return_value={(actor.id, actor.origin_domain): actor}),
    )

    replayed = await tracker_service.create_task(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, snowflake),
        settings(),
        auth(actor),
        EntityRef("50"),
        payload,
    )

    assert replayed["id"] == "52"
    assert replayed["key"] == "RAID-1"
    snowflake.mint.assert_not_awaited()

    existing.client_request_hash = "0" * 64
    with pytest.raises(HTTPException) as conflict:
        await tracker_service.create_task(
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, snowflake),
            settings(),
            auth(actor),
            EntityRef("50"),
            payload,
        )
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "TRACKER_CLIENT_NONCE_CONFLICT"


@pytest.mark.asyncio
async def test_task_edit_permission_distinguishes_own_and_managed_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    actor = user()
    other = user(user_id=9)
    context = TrackerContext(
        access=cast(
            Any,
            SimpleNamespace(guild=SimpleNamespace(), channel=SimpleNamespace()),
        ),
        board=cast(Any, SimpleNamespace()),
        permissions=0,
    )
    task = TrackerTask(
        id=52,
        origin_domain="home.example",
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        lane_id=51,
        lane_domain="home.example",
        number=1,
        title="Owned task",
        priority="none",
        position=0,
        creator_id=actor.id,
        creator_domain=actor.origin_domain,
        created_at=now,
        updated_at=now,
    )
    get_permissions = AsyncMock(
        return_value=int(Permission.VIEW_CHANNEL | Permission.EDIT_OWN_TRACKER_TASKS)
    )
    denied = AsyncMock(
        side_effect=HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    )
    monkeypatch.setattr(tracker_service, "get_permissions", get_permissions)
    monkeypatch.setattr(tracker_service, "require_permissions", denied)

    own = await tracker_service.require_task_edit(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        context,
        actor,
        task,
    )
    assert own & Permission.EDIT_OWN_TRACKER_TASKS
    denied.assert_not_awaited()

    with pytest.raises(HTTPException) as missing:
        await tracker_service.require_task_edit(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            context,
            other,
            task,
        )
    assert missing.value.status_code == 403

    get_permissions.return_value = int(Permission.VIEW_CHANNEL | Permission.MANAGE_TRACKER_TASKS)
    managed = await tracker_service.require_task_edit(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        context,
        other,
        task,
    )
    assert managed & Permission.MANAGE_TRACKER_TASKS


@pytest.mark.asyncio
async def test_assignment_permission_allows_self_service_but_guards_other_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = user()
    context = TrackerContext(
        access=cast(
            Any,
            SimpleNamespace(guild=SimpleNamespace(), channel=SimpleNamespace()),
        ),
        board=cast(Any, SimpleNamespace()),
        permissions=0,
    )
    require = AsyncMock(return_value=int(Permission.ASSIGN_TRACKER_TASKS))
    monkeypatch.setattr(tracker_service, "require_permissions", require)
    actor_ref = (actor.id, actor.origin_domain)

    await tracker_service.require_assignment_permission(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        context,
        actor,
        old=(None, None),
        new=actor_ref,
    )
    await tracker_service.require_assignment_permission(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        context,
        actor,
        old=actor_ref,
        new=(None, None),
    )
    require.assert_not_awaited()

    await tracker_service.require_assignment_permission(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        context,
        actor,
        old=(None, None),
        new=(9, "home.example"),
    )
    require.assert_awaited_once()
    assert require.await_args.args[4] == (Permission.VIEW_CHANNEL | Permission.ASSIGN_TRACKER_TASKS)


@pytest.mark.asyncio
async def test_cross_lane_move_normalizes_both_lanes_and_completion_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    actor = user()
    board = TrackerBoard(
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        key_prefix="RAID",
        next_task_number=5,
        created_at=now,
        updated_at=now,
    )
    source_lane = TrackerLane(
        id=51,
        origin_domain="home.example",
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        name="Doing",
        color=0,
        kind="in_progress",
        completed=False,
        position=0,
        created_at=now,
        updated_at=now,
    )
    target_lane = TrackerLane(
        id=61,
        origin_domain="home.example",
        channel_id=50,
        channel_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        name="Done",
        color=0,
        kind="completed",
        completed=True,
        position=1,
        created_at=now,
        updated_at=now,
    )

    def task(task_id: int, lane: TrackerLane, position: int) -> TrackerTask:
        return TrackerTask(
            id=task_id,
            origin_domain="home.example",
            channel_id=50,
            channel_domain="home.example",
            guild_id=10,
            guild_domain="home.example",
            lane_id=lane.id,
            lane_domain=lane.origin_domain,
            number=task_id,
            title=f"Task {task_id}",
            priority="none",
            position=position,
            creator_id=actor.id,
            creator_domain=actor.origin_domain,
            completed_at=now if lane.completed else None,
            created_at=now,
            updated_at=now,
        )

    moving = task(1, source_lane, 0)
    source_tail = task(2, source_lane, 1)
    target_head = task(3, target_lane, 0)
    target_tail = task(4, target_lane, 1)
    context = TrackerContext(
        access=cast(Any, SimpleNamespace()),
        board=board,
        permissions=int(Permission.EDIT_OWN_TRACKER_TASKS),
    )
    session = SimpleNamespace(add=lambda _row: None, commit=AsyncMock(), refresh=AsyncMock())
    queued: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(tracker_service, "tracker_context", AsyncMock(return_value=context))
    monkeypatch.setattr(
        tracker_service,
        "ordered_tasks",
        AsyncMock(return_value=[moving, source_tail, target_head, target_tail]),
    )
    monkeypatch.setattr(tracker_service, "lane_by_ref", AsyncMock(return_value=target_lane))
    monkeypatch.setattr(tracker_service, "require_task_edit", AsyncMock(return_value=1))
    monkeypatch.setattr(
        tracker_service,
        "task_users",
        AsyncMock(return_value={(actor.id, actor.origin_domain): actor}),
    )
    monkeypatch.setattr(
        tracker_service,
        "queue_context_dispatch",
        lambda _session, _context, event_type, payload: queued.append((event_type, payload)),
    )
    monkeypatch.setattr(tracker_service, "queue_context_federation", AsyncMock())
    monkeypatch.setattr(tracker_service, "wake_context_outboxes", AsyncMock())

    rendered = await tracker_service.move_task(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        settings(),
        auth(actor),
        EntityRef("50"),
        EntityRef("1"),
        TrackerTaskMove(lane_id="61", position=1),
        now.isoformat(),
    )

    assert (moving.lane_id, moving.lane_domain) == (61, "home.example")
    assert source_tail.position == 0
    assert [target_head.position, moving.position, target_tail.position] == [0, 1, 2]
    assert moving.completed_at is not None
    assert rendered["lane_id"] == "61"
    session.commit.assert_awaited_once()
    assert [event_type for event_type, _payload in queued] == [
        "TRACKER_BOARD_UPDATE",
        "TRACKER_TASK_UPDATE",
    ]
    assert queued[0][1]["reason"] == "task_order_updated"


@pytest.mark.asyncio
async def test_bot_tracker_routes_enforce_read_write_and_manage_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorized = AsyncMock(return_value=SimpleNamespace(user=user()))
    monkeypatch.setattr(tracker_api, "bot_auth_for_channel", authorized)
    monkeypatch.setattr(tracker_api, "get_board", AsyncMock(return_value={}))
    monkeypatch.setattr(tracker_api, "create_task", AsyncMock(return_value={}))
    monkeypatch.setattr(tracker_api, "update_board", AsyncMock(return_value={}))
    channel_ref = EntityRef("50")
    dummy = cast(Any, SimpleNamespace())
    config = settings()

    await tracker_api.bot_get_tracker_board(channel_ref, dummy, dummy, dummy, config)
    await tracker_api.bot_post_tracker_task(
        channel_ref,
        TrackerTaskCreate(lane_id="51", title="Bot task"),
        dummy,
        dummy,
        dummy,
        dummy,
        config,
    )
    await tracker_api.bot_patch_tracker_board(
        channel_ref,
        TrackerBoardUpdate(key_prefix="BOT"),
        dummy,
        dummy,
        dummy,
        config,
        '"version"',
    )

    assert [call.args[4:] for call in authorized.await_args_list] == [
        ("tasks.read",),
        ("tasks.write",),
        ("tasks.manage", "tasks.read"),
    ]
    assert event_intent("TRACKER_TASK_CREATE") == "guild_tasks"
    assert event_scope("TRACKER_TASK_CREATE") == "tasks.read"
