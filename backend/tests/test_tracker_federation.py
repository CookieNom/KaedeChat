from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.sql.dml import Delete

import app.api.federation as federation_api
import app.federation.guilds as guild_federation
import app.federation.tracker as tracker_federation
from app.api.dependencies import AuthenticatedUser
from app.auth.tokens import AccessGrant
from app.core.permissions import Permission
from app.core.settings import Settings
from app.db.models import (
    Channel,
    Guild,
    TrackerBoard,
    TrackerDispatchOutbox,
    TrackerLane,
    TrackerTask,
    User,
)
from app.federation.guilds import GUILD_MUTATION_EVENT_TYPES
from app.federation.tracker import (
    TrackerSnapshotChanged,
    apply_tracker_invalidation,
    tracker_snapshot_cursor,
    tracker_snapshot_cursor_task_id,
    tracker_snapshot_page_payload,
    validate_tracker_snapshot,
)
from app.tracker import service as tracker_service
from app.tracker.federation import queue_tracker_federation_invalidation


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


def tracker_rows(
    *,
    domain: str = "remote.example",
) -> tuple[TrackerBoard, TrackerLane, TrackerTask, User, User]:
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    board = TrackerBoard(
        channel_id=20,
        channel_domain=domain,
        guild_id=10,
        guild_domain=domain,
        key_prefix="OPS",
        next_task_number=2,
        created_at=now,
        updated_at=now,
    )
    lane = TrackerLane(
        id=30,
        origin_domain=domain,
        channel_id=20,
        channel_domain=domain,
        guild_id=10,
        guild_domain=domain,
        name="Backlog",
        color=0x3B82F6,
        kind="backlog",
        completed=False,
        position=0,
        created_at=now,
        updated_at=now,
    )
    creator = User(
        id=40,
        origin_domain="creator.example",
        is_local=False,
        username="creator",
        account_type="human",
        profile_resolved=True,
        profile_version=1,
        e2ee_device_generation=0,
    )
    assignee = User(
        id=41,
        origin_domain=domain,
        is_local=False,
        username="assignee",
        account_type="human",
        profile_resolved=True,
        profile_version=1,
        e2ee_device_generation=0,
    )
    task = TrackerTask(
        id=50,
        origin_domain=domain,
        channel_id=20,
        channel_domain=domain,
        guild_id=10,
        guild_domain=domain,
        lane_id=30,
        lane_domain=domain,
        number=1,
        title="Ship federation",
        description="Keep historical creators resolvable.",
        priority="high",
        position=0,
        creator_id=creator.id,
        creator_domain=creator.origin_domain,
        assignee_id=assignee.id,
        assignee_domain=assignee.origin_domain,
        created_at=now,
        updated_at=now,
    )
    return board, lane, task, creator, assignee


def test_tracker_snapshot_validates_historical_creator_and_current_assignee() -> None:
    board, lane, task, creator, assignee = tracker_rows()
    snapshot = tracker_snapshot_page_payload(
        settings(),
        board,
        [lane],
        [task],
        [creator, assignee],
        task_count=1,
        has_more=False,
    )
    validate_tracker_snapshot(
        snapshot,
        expected_guild=(10, "remote.example"),
        expected_channel=(20, "remote.example"),
    )

    without_creator = {**snapshot, "users": snapshot["users"][1:]}
    with pytest.raises(ValueError, match="user profiles"):
        validate_tracker_snapshot(
            without_creator,
            expected_guild=(10, "remote.example"),
            expected_channel=(20, "remote.example"),
        )

    without_membership = {**snapshot, "assignee_member_refs": []}
    with pytest.raises(ValueError, match="assignee memberships"):
        validate_tracker_snapshot(
            without_membership,
            expected_guild=(10, "remote.example"),
            expected_channel=(20, "remote.example"),
        )


def test_tracker_snapshot_cursor_is_authenticated_and_revision_bound() -> None:
    board, _lane, task, _creator, _assignee = tracker_rows()
    config = settings()
    cursor = tracker_snapshot_cursor(config, board, task.id)
    assert tracker_snapshot_cursor_task_id(config, board, cursor) == task.id

    replacement = "A" if cursor[-1] != "A" else "B"
    with pytest.raises(ValueError, match="signature"):
        tracker_snapshot_cursor_task_id(config, board, cursor[:-1] + replacement)

    board.updated_at += timedelta(microseconds=1)
    with pytest.raises(TrackerSnapshotChanged):
        tracker_snapshot_cursor_task_id(config, board, cursor)

    with pytest.raises(ValueError, match="invalid"):
        tracker_snapshot_cursor_task_id(config, board, "%")


class InvalidationSession:
    def __init__(self, channel: Channel, board: TrackerBoard | None = None) -> None:
        self.channel = channel
        self.board = board
        self.executed: list[object] = []
        self.added: list[object] = []

    async def get(self, model: object, key: object, **_kwargs: object) -> object | None:
        del key
        if model is Channel:
            return self.channel
        if model is TrackerBoard:
            return self.board
        return None

    async def scalar(self, _statement: object) -> object | None:
        return None

    async def execute(self, statement: object) -> object:
        self.executed.append(statement)
        return SimpleNamespace()

    async def flush(self) -> None:
        return None

    def add(self, row: object) -> None:
        self.added.append(row)
        if isinstance(row, TrackerBoard):
            self.board = row


@pytest.mark.asyncio
async def test_tracker_invalidation_queue_contract_round_trips_to_durable_applier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board, _lane, _task, _creator, _assignee = tracker_rows()
    guild = cast(
        Guild,
        SimpleNamespace(id=10, origin_domain="remote.example"),
    )
    channel = cast(
        Channel,
        SimpleNamespace(
            id=20,
            origin_domain="remote.example",
            guild_id=10,
            guild_domain="remote.example",
            type=17,
        ),
    )
    actor = cast(User, SimpleNamespace(id=1, origin_domain="remote.example"))
    queued = AsyncMock()
    monkeypatch.setattr("app.tracker.federation.queue_guild_mutation", queued)

    await queue_tracker_federation_invalidation(
        cast(Any, object()),
        settings("remote.example"),
        guild,
        channel,
        actor,
        board,
        reason="task_updated",
    )
    assert queued.await_args.args[4] == "guild.tracker.board.invalidate"
    assert queued.await_args.kwargs["channel"] is channel
    content = queued.await_args.args[5]
    session = InvalidationSession(channel)
    await apply_tracker_invalidation(
        cast(Any, session),
        guild,
        content,
        {
            "channel_id": "20",
            "channel_domain": "remote.example",
        },
    )
    rows = [row for row in session.added if isinstance(row, TrackerDispatchOutbox)]
    assert len(rows) == 1
    assert rows[0].event_type == "TRACKER_BOARD_UPDATE"
    assert rows[0].payload == {
        "channel_id": "20",
        "channel_domain": "remote.example",
        "key_prefix": "OPS",
        "next_task_number": "2",
        "version": board.updated_at.isoformat(),
        "full_refresh": True,
        "reason": "task_updated",
    }


@pytest.mark.parametrize("version_offset", [timedelta(seconds=-1), timedelta(0)])
@pytest.mark.asyncio
async def test_delayed_or_equal_tracker_invalidation_keeps_current_hydration_and_notifies(
    version_offset: timedelta,
) -> None:
    board, _lane, _task, _creator, _assignee = tracker_rows()
    channel = cast(
        Channel,
        SimpleNamespace(
            id=20,
            origin_domain="remote.example",
            guild_id=10,
            guild_domain="remote.example",
            type=17,
        ),
    )
    guild = cast(Guild, SimpleNamespace(id=10, origin_domain="remote.example"))
    session = InvalidationSession(channel, board)
    await apply_tracker_invalidation(
        cast(Any, session),
        guild,
        {
            "tracker": {
                "channel_id": "20",
                "channel_domain": "remote.example",
                "key_prefix": "OPS",
                "next_task_number": "2",
                "version": (board.updated_at + version_offset).isoformat(),
            },
            "reason": "task_updated",
        },
        {"channel_id": "20", "channel_domain": "remote.example"},
    )
    assert len(session.executed) == 1
    assert session.executed[0].table.name == "tracker_dispatch_outbox"
    queued = [row for row in session.added if isinstance(row, TrackerDispatchOutbox)]
    assert len(queued) == 1
    assert queued[0].payload["version"] == board.updated_at.isoformat()


@pytest.mark.asyncio
async def test_tracker_snapshot_rejects_unrelated_peer_before_visibility_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(federation_api, "require_guild_federation_access", lambda _p: None)
    monkeypatch.setattr(
        federation_api,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(
        federation_api,
        "home_guild",
        AsyncMock(return_value=SimpleNamespace(id=10, origin_domain="home.example")),
    )
    unrelated = AsyncMock(
        side_effect=HTTPException(status_code=403, detail={"code": "NOT_A_GUILD_MEMBER"})
    )
    visibility = AsyncMock()
    monkeypatch.setattr(federation_api, "require_origin_guild_member", unrelated)
    monkeypatch.setattr(
        federation_api,
        "cached_visible_guild_channels_for_origin",
        visibility,
    )
    with pytest.raises(HTTPException) as exc:
        await federation_api.federation_tracker_snapshot(
            guild_id=10,
            channel_id=20,
            cursor=None,
            principal=cast(Any, SimpleNamespace(origin="unrelated.example")),
            session=cast(Any, object()),
            redis=cast(Any, object()),
            settings=settings(),
        )
    assert exc.value.status_code == 403
    visibility.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_hydration_runs_only_after_live_tracker_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=1,
        origin_domain="home.example",
        is_local=True,
        username="actor",
        account_type="human",
    )
    auth = AuthenticatedUser(
        actor,
        AccessGrant(actor.id, actor.origin_domain, "session"),
        "",
        False,
    )
    access = SimpleNamespace(
        channel=SimpleNamespace(
            id=20,
            origin_domain="remote.example",
            type=17,
        ),
        guild=SimpleNamespace(id=10, origin_domain="remote.example"),
    )
    monkeypatch.setattr(tracker_service, "load_channel_access", AsyncMock(return_value=access))
    denied = AsyncMock(
        side_effect=HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    )
    hydrate = AsyncMock()
    monkeypatch.setattr(tracker_service, "require_permissions", denied)
    monkeypatch.setattr(tracker_service, "hydrate_replicated_tracker", hydrate)
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as exc:
        await tracker_service.tracker_context(
            cast(Any, session),
            cast(Any, object()),
            settings(),
            auth,
            cast(Any, "20@remote.example"),
            mutation=False,
            needed=Permission.VIEW_CHANNEL,
        )
    assert exc.value.status_code == 403
    hydrate.assert_not_awaited()
    assert "guild.tracker.board.invalidate" in GUILD_MUTATION_EVENT_TYPES


@pytest.mark.asyncio
async def test_tracker_snapshot_pagination_rejects_a_changed_board_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board, lane, first, creator, assignee = tracker_rows()
    board.next_task_number = 3
    second = TrackerTask(
        id=51,
        origin_domain=first.origin_domain,
        channel_id=first.channel_id,
        channel_domain=first.channel_domain,
        guild_id=first.guild_id,
        guild_domain=first.guild_domain,
        lane_id=first.lane_id,
        lane_domain=first.lane_domain,
        number=2,
        title="Finish the cache",
        priority="medium",
        position=1,
        creator_id=creator.id,
        creator_domain=creator.origin_domain,
        created_at=first.created_at,
        updated_at=first.updated_at,
    )
    first_page = tracker_snapshot_page_payload(
        settings(),
        board,
        [lane],
        [first],
        [creator, assignee],
        task_count=2,
        has_more=True,
    )
    board.updated_at += timedelta(microseconds=1)
    changed_page = tracker_snapshot_page_payload(
        settings(),
        board,
        [lane],
        [second],
        [creator],
        task_count=2,
        has_more=False,
    )
    pages = [first_page, changed_page]
    signed_request = AsyncMock(side_effect=[httpx.Response(200, json=page) for page in pages])
    monkeypatch.setattr(tracker_federation, "signed_request", signed_request)
    guild = cast(Guild, SimpleNamespace(id=10, origin_domain="remote.example"))
    channel = cast(Channel, SimpleNamespace(id=20, origin_domain="remote.example"))

    with pytest.raises(TrackerSnapshotChanged):
        await tracker_federation._fetch_tracker_snapshot_once(
            cast(Any, object()),
            settings(),
            guild,
            channel,
            deadline=tracker_federation.time.monotonic() + 10,
        )
    assert signed_request.await_count == 2


@pytest.mark.asyncio
async def test_tracker_hydration_rolls_back_an_incomplete_atomic_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = cast(Guild, SimpleNamespace(id=10, origin_domain="remote.example"))
    channel = cast(
        Channel,
        SimpleNamespace(
            id=20,
            origin_domain="remote.example",
            guild_id=10,
            guild_domain="remote.example",
            type=17,
        ),
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=None),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    monkeypatch.setattr(tracker_federation, "lock_replicated_tracker", AsyncMock())
    monkeypatch.setattr(
        tracker_federation,
        "replicated_tracker_is_stale",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        tracker_federation,
        "fetch_tracker_snapshot",
        AsyncMock(return_value={"board": {}}),
    )
    monkeypatch.setattr(
        tracker_federation,
        "apply_tracker_snapshot",
        AsyncMock(side_effect=RuntimeError("partial replacement")),
    )

    with pytest.raises(HTTPException) as exc:
        await tracker_federation.hydrate_replicated_tracker(
            cast(Any, session),
            cast(Any, object()),
            settings(),
            guild,
            channel,
        )
    assert exc.value.status_code == 503
    assert exc.value.detail == {"code": "FEDERATION_UNAVAILABLE"}
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_remote_tracker_hydration_commits_validated_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board, _lane, _task, _creator, _assignee = tracker_rows()
    guild = cast(Guild, SimpleNamespace(id=10, origin_domain="remote.example"))
    channel = cast(
        Channel,
        SimpleNamespace(
            id=20,
            origin_domain="remote.example",
            guild_id=10,
            guild_domain="remote.example",
            type=17,
        ),
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[None, board]),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    snapshot = {"board": {"version": board.updated_at.isoformat()}}
    config = settings()
    fetch = AsyncMock(return_value=snapshot)
    apply = AsyncMock(return_value=board)
    lock = AsyncMock()
    monkeypatch.setattr(tracker_federation, "lock_replicated_tracker", lock)
    monkeypatch.setattr(
        tracker_federation,
        "replicated_tracker_is_stale",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(tracker_federation, "fetch_tracker_snapshot", fetch)
    monkeypatch.setattr(tracker_federation, "apply_tracker_snapshot", apply)

    hydrated = await tracker_federation.hydrate_replicated_tracker(
        cast(Any, session),
        cast(Any, object()),
        config,
        guild,
        channel,
    )

    assert hydrated is board
    fetch.assert_awaited_once_with(session, config, guild, channel)
    apply.assert_awaited_once_with(session, config, guild, channel, snapshot)
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    # The first transaction serializes replacement; the second lock remains
    # held while the caller renders the board, closing the post-commit race.
    assert lock.await_count == 2


class PurgeSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []

    async def execute(self, statement: Any) -> list[object]:
        self.executed.append(statement)
        return []

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_inaccessible_remote_channel_prunes_its_tracker_cache() -> None:
    channel = Channel(
        id=20,
        origin_domain="remote.example",
        guild_id=10,
        guild_domain="remote.example",
        type=17,
        name="Operations",
        unavailable=False,
    )
    session = PurgeSession()
    await guild_federation.purge_replicated_channel_cache(
        cast(Any, session),
        settings(),
        channel,
        reconcile=False,
    )

    tracker_deletes = [
        statement
        for statement in session.executed
        if isinstance(statement, Delete) and statement.table.name == "tracker_boards"
    ]
    assert len(tracker_deletes) == 1
    assert channel.unavailable is True
