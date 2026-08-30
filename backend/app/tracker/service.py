from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedUser
from app.chat.channel_access import (
    ChannelAccess,
    load_channel_access,
    lock_local_channel_mutation,
)
from app.chat.guild_revision import wake_queued_guild_federation
from app.chat.permissions import get_permissions, require_permissions
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef, EntityReferenceLike
from app.db.models import (
    Channel,
    GuildMember,
    TrackerBoard,
    TrackerLane,
    TrackerTask,
    User,
)
from app.federation.guild_management import (
    GuildManagementOperation,
    proxy_remote_guild_management,
)
from app.federation.tracker import hydrate_replicated_tracker
from app.tracker.federation import queue_tracker_federation_invalidation
from app.tracker.outbox import queue_tracker_dispatch, wake_tracker_dispatch_outbox
from app.tracker.payloads import (
    UserKey,
    tracker_board_payload,
    tracker_lane_event_payload,
    tracker_lane_payload,
    tracker_task_event_payload,
    tracker_task_payload,
)
from app.tracker.schemas import (
    TrackerBoardUpdate,
    TrackerLaneCreate,
    TrackerLaneMove,
    TrackerLaneUpdate,
    TrackerTaskCreate,
    TrackerTaskMove,
    TrackerTaskUpdate,
)

TRACKER_CHANNEL_TYPE = 17
MAX_TRACKER_LANES = 50
MAX_TRACKER_TASKS = 5_000
DEFAULT_TRACKER_LANES = (
    ("Backlog", 0x3B82F6, "backlog", False),
    ("Planned", 0xF59E0B, "planned", False),
    ("In progress", 0xA3E635, "in_progress", False),
    ("Done", 0x22C55E, "completed", True),
)


@dataclass(slots=True)
class TrackerContext:
    access: ChannelAccess
    board: TrackerBoard
    permissions: int


class PositionedTrackerResource(Protocol):
    position: int
    updated_at: datetime


async def proxy_remote_tracker_mutation(
    session: AsyncSession,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    operation: GuildManagementOperation,
    payload: dict[str, object],
    *,
    returns_body: bool = True,
) -> tuple[bool, dict[str, object]]:
    """Route a human mutation to the tracker channel's guild authority.

    Bot requests are deliberately not relayed: their token and DPoP proof are
    target-audience bound, so bot clients must address the qualified channel's
    authority directly. Human requests use the signed guild-management RPC and
    are authorized again against authoritative guild state at the destination.
    """

    _channel_id, channel_domain = channel_ref.resolve(settings.domain)
    if channel_domain == settings.domain:
        return False, {}
    if auth.user.account_type == "bot":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BOT_RESOURCE_AUTHORITY_REQUIRED",
                "resource_ref": str(channel_ref),
                "authority_domain": channel_domain,
            },
        )
    access = await load_channel_access(session, settings, auth.user, channel_ref)
    if access.channel.type != TRACKER_CHANNEL_TYPE or access.guild is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_NOT_FOUND"})
    guild_ref = EntityRef(f"{access.guild.id}@{access.guild.origin_domain}")
    result = await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        auth.user,
        operation,
        {
            "channel_ref": f"{access.channel.id}@{access.channel.origin_domain}",
            **payload,
        },
    )
    if result is None:
        raise RuntimeError("remote tracker mutation resolved to a local guild")
    if not returns_body:
        if result.body is not None:
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
            )
        return True, {}
    if not isinstance(result.body, dict):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
        )
    return True, result.body


def default_key_prefix(name: str, channel_id: int) -> str:
    words = [
        "".join(
            character for character in word.upper() if character.isascii() and character.isalnum()
        )
        for word in name.split()
    ]
    words = [word for word in words if word]
    if len(words) > 1:
        candidate = "".join(word[0] for word in words)[:10]
    else:
        candidate = words[0][:10] if words else ""
    if not candidate or not candidate[0].isalpha():
        candidate = f"KT{str(channel_id)[-8:]}"[:10]
    if len(candidate) < 2:
        candidate = (candidate + "K")[:2]
    return candidate


def task_request_fingerprint(payload: TrackerTaskCreate) -> str:
    """Return the stable semantic fingerprint bound to a task client nonce."""

    canonical = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


async def create_tracker_state(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    channel: Channel,
    *,
    key_prefix: str | None = None,
) -> TrackerBoard:
    if channel.type != TRACKER_CHANNEL_TYPE or channel.guild_id is None or channel.name is None:
        raise ValueError("tracker state requires a named guild tracker channel")
    board = TrackerBoard(
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        guild_id=channel.guild_id,
        guild_domain=channel.guild_domain,
        key_prefix=key_prefix or default_key_prefix(channel.name, channel.id),
        next_task_number=1,
    )
    session.add(board)
    for position, (name, color, kind, completed) in enumerate(DEFAULT_TRACKER_LANES):
        session.add(
            TrackerLane(
                id=await snowflake.mint(),
                origin_domain=channel.origin_domain,
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                guild_id=channel.guild_id,
                guild_domain=channel.guild_domain,
                name=name,
                color=color,
                kind=kind,
                completed=completed,
                position=position,
            )
        )
    return board


def require_tracker_version(updated_at: datetime, if_match: str | None) -> None:
    current = updated_at.isoformat()
    if if_match is None:
        raise HTTPException(
            status_code=428,
            detail={"code": "TRACKER_VERSION_REQUIRED", "current_version": current},
        )
    if if_match.strip('"') != current:
        raise HTTPException(
            status_code=412,
            detail={"code": "TRACKER_VERSION_CONFLICT", "current_version": current},
        )


async def tracker_context(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    *,
    mutation: bool,
    needed: Permission,
) -> TrackerContext:
    access = await load_channel_access(session, settings, auth.user, channel_ref)
    if access.channel.type != TRACKER_CHANNEL_TYPE or access.guild is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_NOT_FOUND"})
    if mutation:
        access = await lock_local_channel_mutation(session, settings, access)
        if access.guild is None or access.guild.origin_domain != settings.domain:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TRACKER_AUTHORITY_REQUIRED",
                    "authority_domain": (
                        access.guild.origin_domain if access.guild is not None else None
                    ),
                },
            )
    query = select(TrackerBoard).where(
        TrackerBoard.channel_id == access.channel.id,
        TrackerBoard.channel_domain == access.channel.origin_domain,
    )
    if mutation:
        query = query.with_for_update()
    board = await session.scalar(query)
    guild = access.guild
    if guild is None:
        raise RuntimeError("tracker channel is not guild-scoped")
    permissions = await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        needed,
        channel=access.channel,
    )
    if guild.origin_domain != settings.domain:
        # Do not let an unauthorized local actor trigger outbound federation
        # work. The live permission calculation above precedes every lazy
        # hydration attempt, including an explicitly stale placeholder board.
        board = await hydrate_replicated_tracker(
            session,
            redis,
            settings,
            guild,
            access.channel,
        )
    elif board is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_NOT_FOUND"})
    return TrackerContext(access, board, permissions)


async def ordered_lanes(
    session: AsyncSession, board: TrackerBoard, *, lock: bool = False
) -> list[TrackerLane]:
    query = (
        select(TrackerLane)
        .where(
            TrackerLane.channel_id == board.channel_id,
            TrackerLane.channel_domain == board.channel_domain,
        )
        .order_by(TrackerLane.position, TrackerLane.id)
    )
    if lock:
        query = query.with_for_update()
    return list(await session.scalars(query))


async def ordered_tasks(
    session: AsyncSession,
    board: TrackerBoard,
    *,
    lane: TrackerLane | None = None,
    lock: bool = False,
) -> list[TrackerTask]:
    query = select(TrackerTask).where(
        TrackerTask.channel_id == board.channel_id,
        TrackerTask.channel_domain == board.channel_domain,
    )
    if lane is not None:
        query = query.where(
            TrackerTask.lane_id == lane.id,
            TrackerTask.lane_domain == lane.origin_domain,
        )
    query = query.order_by(TrackerTask.lane_id, TrackerTask.position, TrackerTask.id)
    if lock:
        query = query.with_for_update()
    return list(await session.scalars(query))


async def task_users(session: AsyncSession, tasks: list[TrackerTask]) -> dict[UserKey, User]:
    keys: set[UserKey] = {(task.creator_id, task.creator_domain) for task in tasks}
    keys.update(
        (task.assignee_id, task.assignee_domain)
        for task in tasks
        if task.assignee_id is not None and task.assignee_domain is not None
    )
    if not keys:
        return {}
    users = list(
        await session.scalars(select(User).where(tuple_(User.id, User.origin_domain).in_(keys)))
    )
    return {(user.id, user.origin_domain): user for user in users}


async def board_response(session: AsyncSession, context: TrackerContext) -> dict[str, object]:
    lanes = await ordered_lanes(session, context.board)
    tasks = await ordered_tasks(session, context.board)
    if len(lanes) > MAX_TRACKER_LANES or len(tasks) > MAX_TRACKER_TASKS:
        raise HTTPException(status_code=409, detail={"code": "TRACKER_CAPACITY_INVALID"})
    users = await task_users(session, tasks)
    return tracker_board_payload(
        context.board,
        lanes,
        tasks,
        users,
        permissions=context.permissions,
    )


async def get_board(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
) -> dict[str, object]:
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=False,
        needed=required_permissions("tracker.read"),
    )
    return await board_response(session, context)


def queue_board_update(
    session: AsyncSession,
    context: TrackerContext,
    *,
    reason: str,
) -> None:
    queue_context_dispatch(
        session,
        context,
        "TRACKER_BOARD_UPDATE",
        {
            "channel_id": str(context.board.channel_id),
            "channel_domain": context.board.channel_domain,
            "key_prefix": context.board.key_prefix,
            "next_task_number": str(context.board.next_task_number),
            "version": context.board.updated_at.isoformat(),
            # Board events are explicit invalidations. Some board mutations
            # (for example toggling a completed lane) atomically update many
            # task representations and must not fan out thousands of gateway
            # events. Clients should re-fetch the board on this event.
            "full_refresh": True,
            "reason": reason,
        },
    )


def queue_context_dispatch(
    session: AsyncSession,
    context: TrackerContext,
    event_type: str,
    payload: dict[str, object],
) -> None:
    guild = context.access.guild
    if guild is None:
        raise RuntimeError("tracker dispatch requires a guild")
    queue_tracker_dispatch(
        session,
        channel_id=context.board.channel_id,
        channel_domain=context.board.channel_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        event_type=event_type,
        payload=payload,
    )


async def queue_context_federation(
    session: AsyncSession,
    settings: Settings,
    context: TrackerContext,
    actor: User,
    *,
    reason: str,
) -> None:
    guild = context.access.guild
    if guild is None:
        raise RuntimeError("tracker federation requires a guild")
    await queue_tracker_federation_invalidation(
        session,
        settings,
        guild,
        context.access.channel,
        actor,
        context.board,
        reason=reason,
    )


async def wake_context_outboxes(context: TrackerContext) -> None:
    guild = context.access.guild
    if guild is None:
        raise RuntimeError("tracker outbox wake requires a guild")
    await wake_tracker_dispatch_outbox()
    await wake_queued_guild_federation(guild)


async def update_board(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    payload: TrackerBoardUpdate,
    if_match: str | None,
) -> dict[str, object]:
    proxied, body = await proxy_remote_tracker_mutation(
        session,
        settings,
        auth,
        channel_ref,
        "tracker.board.update",
        {
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "if_match": if_match,
        },
    )
    if proxied:
        return body
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=True,
        needed=required_permissions("tracker.settings.manage"),
    )
    require_tracker_version(context.board.updated_at, if_match)
    if context.board.key_prefix != payload.key_prefix:
        context.board.key_prefix = payload.key_prefix
        context.board.updated_at = next_tracker_version(context.board.updated_at)
        await queue_context_federation(
            session, settings, context, auth.user, reason="settings_updated"
        )
        queue_board_update(session, context, reason="settings_updated")
        await session.commit()
        await session.refresh(context.board)
        await wake_context_outboxes(context)
    return await board_response(session, context)


async def lane_by_ref(
    session: AsyncSession,
    board: TrackerBoard,
    lane_ref: EntityReferenceLike,
    settings: Settings,
) -> TrackerLane:
    lane_id, lane_domain = lane_ref.resolve(settings.domain)
    if lane_domain != board.channel_domain:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_LANE_NOT_FOUND"})
    lane = await session.scalar(
        select(TrackerLane)
        .where(
            TrackerLane.id == lane_id,
            TrackerLane.origin_domain == lane_domain,
            TrackerLane.channel_id == board.channel_id,
            TrackerLane.channel_domain == board.channel_domain,
        )
        .with_for_update()
    )
    if lane is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_LANE_NOT_FOUND"})
    return lane


def next_tracker_version(current: datetime, *, now: datetime | None = None) -> datetime:
    """Return a strict successor for optimistic and federation version fences."""

    candidate = now or datetime.now(UTC)
    return max(candidate, current + timedelta(microseconds=1))


def apply_positions(
    items: Sequence[PositionedTrackerResource], *, updated_at: datetime | None = None
) -> None:
    now = updated_at or datetime.now(UTC)
    for position, item in enumerate(items):
        if item.position != position:
            item.position = position
            item.updated_at = now


def other_positions_need_normalization(
    items: Sequence[PositionedTrackerResource],
    *,
    target: PositionedTrackerResource | None = None,
) -> bool:
    return any(
        item is not target and item.position != position for position, item in enumerate(items)
    )


async def create_lane(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    payload: TrackerLaneCreate,
) -> dict[str, object]:
    proxied, body = await proxy_remote_tracker_mutation(
        session,
        settings,
        auth,
        channel_ref,
        "tracker.lane.create",
        {"data": payload.model_dump(mode="json", exclude_unset=True)},
    )
    if proxied:
        return body
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=True,
        needed=required_permissions("tracker.lane.manage"),
    )
    lanes = await ordered_lanes(session, context.board, lock=True)
    if len(lanes) >= MAX_TRACKER_LANES:
        raise HTTPException(status_code=409, detail={"code": "TRACKER_LANE_LIMIT_REACHED"})
    position = len(lanes) if payload.position is None else payload.position
    if position > len(lanes):
        raise HTTPException(status_code=400, detail={"code": "TRACKER_POSITION_INVALID"})
    lane = TrackerLane(
        id=await snowflake.mint(),
        origin_domain=context.board.channel_domain,
        channel_id=context.board.channel_id,
        channel_domain=context.board.channel_domain,
        guild_id=context.board.guild_id,
        guild_domain=context.board.guild_domain,
        name=payload.name,
        color=payload.color,
        kind=payload.kind,
        completed=payload.completed,
        position=position,
    )
    lanes.insert(position, lane)
    positions_shifted = other_positions_need_normalization(lanes, target=lane)
    mutation_version = next_tracker_version(context.board.updated_at)
    apply_positions(lanes, updated_at=mutation_version)
    context.board.updated_at = mutation_version
    session.add(lane)
    # Give event payloads stable timestamps before the transaction is committed.
    lane.created_at = context.board.updated_at
    lane.updated_at = context.board.updated_at
    await queue_context_federation(
        session,
        settings,
        context,
        auth.user,
        reason="lane_order_updated" if positions_shifted else "lane_created",
    )
    if positions_shifted:
        queue_board_update(session, context, reason="lane_order_updated")
    queue_context_dispatch(
        session,
        context,
        "TRACKER_LANE_CREATE",
        tracker_lane_event_payload(lane, context.board, task_count=0),
    )
    await session.commit()
    await session.refresh(lane)
    await session.refresh(context.board)
    rendered = tracker_lane_payload(lane, task_count=0)
    await wake_context_outboxes(context)
    return rendered


async def update_lane(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    lane_ref: EntityReferenceLike,
    payload: TrackerLaneUpdate,
    if_match: str | None,
) -> dict[str, object]:
    proxied, body = await proxy_remote_tracker_mutation(
        session,
        settings,
        auth,
        channel_ref,
        "tracker.lane.update",
        {
            "resource_ref": str(lane_ref),
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "if_match": if_match,
        },
    )
    if proxied:
        return body
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=True,
        needed=required_permissions("tracker.lane.manage"),
    )
    lane = await lane_by_ref(session, context.board, lane_ref, settings)
    require_tracker_version(lane.updated_at, if_match)
    values = payload.model_dump(exclude_unset=True)
    completed_changed = "completed" in values and values["completed"] != lane.completed
    changed = False
    for field, value in values.items():
        if getattr(lane, field) != value:
            setattr(lane, field, value)
            changed = True
    tasks: list[TrackerTask] = []
    if completed_changed:
        tasks = await ordered_tasks(session, context.board, lane=lane, lock=True)
    if changed:
        mutation_version = next_tracker_version(context.board.updated_at)
        context.board.updated_at = mutation_version
        lane.updated_at = mutation_version
        if completed_changed:
            for task in tasks:
                task.completed_at = mutation_version if lane.completed else None
                task.updated_at = mutation_version
        await queue_context_federation(
            session,
            settings,
            context,
            auth.user,
            reason="lane_completion_updated" if completed_changed else "lane_updated",
        )
        if completed_changed:
            queue_board_update(session, context, reason="lane_completion_updated")
        else:
            task_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TrackerTask)
                    .where(
                        TrackerTask.lane_id == lane.id,
                        TrackerTask.lane_domain == lane.origin_domain,
                    )
                )
                or 0
            )
            queue_context_dispatch(
                session,
                context,
                "TRACKER_LANE_UPDATE",
                tracker_lane_event_payload(lane, context.board, task_count=task_count),
            )
        await session.commit()
        await session.refresh(lane)
        await session.refresh(context.board)
        await wake_context_outboxes(context)
    task_count = int(
        await session.scalar(
            select(func.count())
            .select_from(TrackerTask)
            .where(
                TrackerTask.lane_id == lane.id,
                TrackerTask.lane_domain == lane.origin_domain,
            )
        )
        or 0
    )
    return tracker_lane_payload(lane, task_count=task_count)


async def move_lane(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    lane_ref: EntityReferenceLike,
    payload: TrackerLaneMove,
    if_match: str | None,
) -> dict[str, object]:
    proxied, body = await proxy_remote_tracker_mutation(
        session,
        settings,
        auth,
        channel_ref,
        "tracker.lane.move",
        {
            "resource_ref": str(lane_ref),
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "if_match": if_match,
        },
    )
    if proxied:
        return body
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=True,
        needed=required_permissions("tracker.lane.manage"),
    )
    lanes = await ordered_lanes(session, context.board, lock=True)
    lane_id, lane_domain = lane_ref.resolve(settings.domain)
    lane = next(
        (item for item in lanes if (item.id, item.origin_domain) == (lane_id, lane_domain)), None
    )
    if lane is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_LANE_NOT_FOUND"})
    require_tracker_version(lane.updated_at, if_match)
    if payload.position >= len(lanes):
        raise HTTPException(status_code=400, detail={"code": "TRACKER_POSITION_INVALID"})
    if lane.position != payload.position:
        lanes.remove(lane)
        lanes.insert(payload.position, lane)
        positions_shifted = other_positions_need_normalization(lanes, target=lane)
        mutation_version = next_tracker_version(context.board.updated_at)
        apply_positions(lanes, updated_at=mutation_version)
        context.board.updated_at = mutation_version
        task_count = int(
            await session.scalar(
                select(func.count())
                .select_from(TrackerTask)
                .where(
                    TrackerTask.lane_id == lane.id, TrackerTask.lane_domain == lane.origin_domain
                )
            )
            or 0
        )
        await queue_context_federation(
            session, settings, context, auth.user, reason="lane_order_updated"
        )
        if positions_shifted:
            queue_board_update(session, context, reason="lane_order_updated")
        queue_context_dispatch(
            session,
            context,
            "TRACKER_LANE_UPDATE",
            tracker_lane_event_payload(lane, context.board, task_count=task_count),
        )
        await session.commit()
        await session.refresh(lane)
        await session.refresh(context.board)
        await wake_context_outboxes(context)
    task_count = int(
        await session.scalar(
            select(func.count())
            .select_from(TrackerTask)
            .where(TrackerTask.lane_id == lane.id, TrackerTask.lane_domain == lane.origin_domain)
        )
        or 0
    )
    return tracker_lane_payload(lane, task_count=task_count)


async def delete_lane(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    lane_ref: EntityReferenceLike,
    if_match: str | None,
) -> None:
    proxied, _body = await proxy_remote_tracker_mutation(
        session,
        settings,
        auth,
        channel_ref,
        "tracker.lane.delete",
        {"resource_ref": str(lane_ref), "if_match": if_match},
        returns_body=False,
    )
    if proxied:
        return
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=True,
        needed=required_permissions("tracker.lane.manage"),
    )
    lanes = await ordered_lanes(session, context.board, lock=True)
    lane_id, lane_domain = lane_ref.resolve(settings.domain)
    lane = next(
        (item for item in lanes if (item.id, item.origin_domain) == (lane_id, lane_domain)), None
    )
    if lane is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_LANE_NOT_FOUND"})
    require_tracker_version(lane.updated_at, if_match)
    if len(lanes) == 1:
        raise HTTPException(status_code=409, detail={"code": "TRACKER_LAST_LANE"})
    if (
        await session.scalar(
            select(TrackerTask.id)
            .where(TrackerTask.lane_id == lane.id, TrackerTask.lane_domain == lane.origin_domain)
            .limit(1)
        )
        is not None
    ):
        raise HTTPException(status_code=409, detail={"code": "TRACKER_LANE_NOT_EMPTY"})
    deleted: dict[str, object] = {
        "channel_id": str(context.board.channel_id),
        "channel_domain": context.board.channel_domain,
        "lane_id": str(lane.id),
        "lane_domain": lane.origin_domain,
    }
    lanes.remove(lane)
    positions_shifted = other_positions_need_normalization(lanes)
    mutation_version = next_tracker_version(context.board.updated_at)
    apply_positions(lanes, updated_at=mutation_version)
    context.board.updated_at = mutation_version
    await session.delete(lane)
    deleted["board_version"] = context.board.updated_at.isoformat()
    await queue_context_federation(
        session,
        settings,
        context,
        auth.user,
        reason="lane_order_updated" if positions_shifted else "lane_deleted",
    )
    if positions_shifted:
        queue_board_update(session, context, reason="lane_order_updated")
    queue_context_dispatch(session, context, "TRACKER_LANE_DELETE", deleted)
    await session.commit()
    await wake_context_outboxes(context)


async def task_by_ref(
    session: AsyncSession,
    board: TrackerBoard,
    task_ref: EntityReferenceLike,
    settings: Settings,
) -> TrackerTask:
    task_id, task_domain = task_ref.resolve(settings.domain)
    if task_domain != board.channel_domain:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_TASK_NOT_FOUND"})
    task = await session.scalar(
        select(TrackerTask)
        .where(
            TrackerTask.id == task_id,
            TrackerTask.origin_domain == task_domain,
            TrackerTask.channel_id == board.channel_id,
            TrackerTask.channel_domain == board.channel_domain,
        )
        .with_for_update()
    )
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_TASK_NOT_FOUND"})
    return task


async def require_task_edit(
    session: AsyncSession, redis: Redis, context: TrackerContext, actor: User, task: TrackerTask
) -> int:
    guild = context.access.guild
    if guild is None:
        raise RuntimeError("tracker channel is not guild-scoped")
    permissions = await get_permissions(
        session, redis, guild, actor, channel=context.access.channel
    )
    own = (task.creator_id, task.creator_domain) == (actor.id, actor.origin_domain) or (
        task.assignee_id,
        task.assignee_domain,
    ) == (actor.id, actor.origin_domain)
    if permissions & required_permissions("tracker.task.update.other") == required_permissions(
        "tracker.task.update.other"
    ):
        return permissions
    if own and permissions & required_permissions(
        "tracker.task.update.own"
    ) == required_permissions("tracker.task.update.own"):
        return permissions
    await require_permissions(
        session,
        redis,
        guild,
        actor,
        required_permissions("tracker.task.update.own" if own else "tracker.task.update.other"),
        channel=context.access.channel,
    )
    raise AssertionError("permission guard unexpectedly returned")


async def member_user(
    session: AsyncSession, board: TrackerBoard, ref: EntityReferenceLike, settings: Settings
) -> User:
    user_id, user_domain = ref.resolve(settings.domain)
    member = await session.get(
        GuildMember, (board.guild_id, board.guild_domain, user_id, user_domain)
    )
    if member is None:
        raise HTTPException(status_code=400, detail={"code": "TRACKER_ASSIGNEE_NOT_MEMBER"})
    user = await session.get(User, (user_id, user_domain))
    if user is None:
        raise HTTPException(status_code=400, detail={"code": "TRACKER_ASSIGNEE_NOT_MEMBER"})
    return user


async def require_assignment_permission(
    session: AsyncSession,
    redis: Redis,
    context: TrackerContext,
    actor: User,
    *,
    old: tuple[int | None, str | None],
    new: tuple[int | None, str | None],
) -> None:
    actor_ref = (actor.id, actor.origin_domain)
    self_only = (old == (None, None) and new == actor_ref) or (
        old == actor_ref and new == (None, None)
    )
    if old == new or self_only:
        return
    guild = context.access.guild
    if guild is None:
        raise RuntimeError("tracker channel is not guild-scoped")
    await require_permissions(
        session,
        redis,
        guild,
        actor,
        required_permissions("tracker.task.assign"),
        channel=context.access.channel,
    )


async def create_task(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    payload: TrackerTaskCreate,
) -> dict[str, object]:
    proxied, body = await proxy_remote_tracker_mutation(
        session,
        settings,
        auth,
        channel_ref,
        "tracker.task.create",
        {"data": payload.model_dump(mode="json", exclude_unset=True)},
    )
    if proxied:
        return body
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=True,
        needed=required_permissions("tracker.task.create"),
    )
    request_hash = task_request_fingerprint(payload)
    if payload.client_nonce is not None:
        existing = await session.scalar(
            select(TrackerTask).where(
                TrackerTask.channel_id == context.board.channel_id,
                TrackerTask.channel_domain == context.board.channel_domain,
                TrackerTask.creator_id == auth.user.id,
                TrackerTask.creator_domain == auth.user.origin_domain,
                TrackerTask.client_nonce == payload.client_nonce,
            )
        )
        if existing is not None:
            if existing.client_request_hash != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "TRACKER_CLIENT_NONCE_CONFLICT"},
                )
            users = await task_users(session, [existing])
            return tracker_task_payload(existing, context.board, users)
    tasks = await ordered_tasks(session, context.board, lock=True)
    if len(tasks) >= MAX_TRACKER_TASKS:
        raise HTTPException(status_code=409, detail={"code": "TRACKER_TASK_LIMIT_REACHED"})
    lane = await lane_by_ref(session, context.board, payload.lane_id, settings)
    lane_tasks = [
        task for task in tasks if (task.lane_id, task.lane_domain) == (lane.id, lane.origin_domain)
    ]
    position = len(lane_tasks) if payload.position is None else payload.position
    if position > len(lane_tasks):
        raise HTTPException(status_code=400, detail={"code": "TRACKER_POSITION_INVALID"})
    assignee: User | None = None
    if payload.assignee_id is not None:
        assignee = await member_user(session, context.board, payload.assignee_id, settings)
        await require_assignment_permission(
            session,
            redis,
            context,
            auth.user,
            old=(None, None),
            new=(assignee.id, assignee.origin_domain),
        )
    now = next_tracker_version(context.board.updated_at)
    task = TrackerTask(
        id=await snowflake.mint(),
        origin_domain=context.board.channel_domain,
        channel_id=context.board.channel_id,
        channel_domain=context.board.channel_domain,
        guild_id=context.board.guild_id,
        guild_domain=context.board.guild_domain,
        lane_id=lane.id,
        lane_domain=lane.origin_domain,
        number=context.board.next_task_number,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        position=position,
        due_at=payload.due_at,
        completed_at=now if lane.completed else None,
        creator_id=auth.user.id,
        creator_domain=auth.user.origin_domain,
        assignee_id=assignee.id if assignee is not None else None,
        assignee_domain=assignee.origin_domain if assignee is not None else None,
        client_nonce=payload.client_nonce,
        client_request_hash=request_hash if payload.client_nonce is not None else None,
    )
    lane_tasks.insert(position, task)
    positions_shifted = other_positions_need_normalization(lane_tasks, target=task)
    apply_positions(lane_tasks, updated_at=now)
    context.board.next_task_number += 1
    context.board.updated_at = now
    session.add(task)
    task.created_at = now
    task.updated_at = now
    users = {(auth.user.id, auth.user.origin_domain): auth.user}
    if assignee is not None:
        users[(assignee.id, assignee.origin_domain)] = assignee
    await queue_context_federation(
        session,
        settings,
        context,
        auth.user,
        reason="task_order_updated" if positions_shifted else "task_created",
    )
    if positions_shifted:
        queue_board_update(session, context, reason="task_order_updated")
    queue_context_dispatch(
        session,
        context,
        "TRACKER_TASK_CREATE",
        tracker_task_event_payload(task, context.board, users),
    )
    await session.commit()
    await session.refresh(task)
    await session.refresh(context.board)
    rendered = tracker_task_payload(task, context.board, users)
    await wake_context_outboxes(context)
    return rendered


async def update_task(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    task_ref: EntityReferenceLike,
    payload: TrackerTaskUpdate,
    if_match: str | None,
) -> dict[str, object]:
    proxied, body = await proxy_remote_tracker_mutation(
        session,
        settings,
        auth,
        channel_ref,
        "tracker.task.update",
        {
            "resource_ref": str(task_ref),
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "if_match": if_match,
        },
    )
    if proxied:
        return body
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=True,
        needed=required_permissions("tracker.read"),
    )
    task = await task_by_ref(session, context.board, task_ref, settings)
    require_tracker_version(task.updated_at, if_match)
    await require_task_edit(session, redis, context, auth.user, task)
    values = payload.model_dump(exclude_unset=True)
    users: dict[UserKey, User] = {(auth.user.id, auth.user.origin_domain): auth.user}
    assignee_changed = False
    if "assignee_id" in values:
        assignee = (
            await member_user(session, context.board, payload.assignee_id, settings)
            if payload.assignee_id is not None
            else None
        )
        new_ref = (assignee.id, assignee.origin_domain) if assignee is not None else (None, None)
        await require_assignment_permission(
            session,
            redis,
            context,
            auth.user,
            old=(task.assignee_id, task.assignee_domain),
            new=new_ref,
        )
        old_ref = (task.assignee_id, task.assignee_domain)
        task.assignee_id, task.assignee_domain = new_ref
        assignee_changed = old_ref != new_ref
        values.pop("assignee_id", None)
        if assignee is not None:
            users[(assignee.id, assignee.origin_domain)] = assignee
    changed = False
    for field, value in values.items():
        if getattr(task, field) != value:
            setattr(task, field, value)
            changed = True
    changed = changed or assignee_changed
    if changed:
        context.board.updated_at = next_tracker_version(context.board.updated_at)
    users.update(await task_users(session, [task]))
    if changed:
        task.updated_at = context.board.updated_at
        await queue_context_federation(session, settings, context, auth.user, reason="task_updated")
        queue_context_dispatch(
            session,
            context,
            "TRACKER_TASK_UPDATE",
            tracker_task_event_payload(task, context.board, users),
        )
        await session.commit()
        await session.refresh(task)
        await session.refresh(context.board)
        await wake_context_outboxes(context)
    rendered = tracker_task_payload(task, context.board, users)
    return rendered


async def move_task(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    task_ref: EntityReferenceLike,
    payload: TrackerTaskMove,
    if_match: str | None,
) -> dict[str, object]:
    proxied, body = await proxy_remote_tracker_mutation(
        session,
        settings,
        auth,
        channel_ref,
        "tracker.task.move",
        {
            "resource_ref": str(task_ref),
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "if_match": if_match,
        },
    )
    if proxied:
        return body
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=True,
        needed=required_permissions("tracker.read"),
    )
    tasks = await ordered_tasks(session, context.board, lock=True)
    task_id, task_domain = task_ref.resolve(settings.domain)
    task = next(
        (item for item in tasks if (item.id, item.origin_domain) == (task_id, task_domain)), None
    )
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_TASK_NOT_FOUND"})
    require_tracker_version(task.updated_at, if_match)
    await require_task_edit(session, redis, context, auth.user, task)
    target_lane = await lane_by_ref(session, context.board, payload.lane_id, settings)
    source_ref = (task.lane_id, task.lane_domain)
    target_ref = (target_lane.id, target_lane.origin_domain)
    source_tasks = [
        item
        for item in tasks
        if (item.lane_id, item.lane_domain) == source_ref and item is not task
    ]
    target_tasks = (
        source_tasks
        if source_ref == target_ref
        else [item for item in tasks if (item.lane_id, item.lane_domain) == target_ref]
    )
    max_position = len(target_tasks)
    if payload.position > max_position:
        raise HTTPException(status_code=400, detail={"code": "TRACKER_POSITION_INVALID"})
    position_changed = source_ref != target_ref or task.position != payload.position
    completion_changed = target_lane.completed != (task.completed_at is not None)
    mutation_version = (
        next_tracker_version(context.board.updated_at)
        if position_changed or completion_changed
        else None
    )
    if source_ref != target_ref:
        task.lane_id, task.lane_domain = target_ref
    if position_changed:
        target_tasks.insert(payload.position, task)
        positions_shifted = other_positions_need_normalization(
            source_tasks,
            target=task,
        ) or (
            target_tasks is not source_tasks
            and other_positions_need_normalization(target_tasks, target=task)
        )
        apply_positions(source_tasks, updated_at=mutation_version)
        apply_positions(target_tasks, updated_at=mutation_version)
    else:
        positions_shifted = False
    if completion_changed:
        task.completed_at = mutation_version if target_lane.completed else None
    if position_changed or completion_changed:
        if mutation_version is None:
            raise AssertionError("changed tracker task lacks a mutation version")
        context.board.updated_at = mutation_version
        task.updated_at = mutation_version
    users = await task_users(session, [task])
    if position_changed or completion_changed:
        await queue_context_federation(
            session,
            settings,
            context,
            auth.user,
            reason="task_order_updated" if position_changed else "task_updated",
        )
        if positions_shifted:
            queue_board_update(session, context, reason="task_order_updated")
        queue_context_dispatch(
            session,
            context,
            "TRACKER_TASK_UPDATE",
            tracker_task_event_payload(task, context.board, users),
        )
        await session.commit()
        await session.refresh(task)
        await session.refresh(context.board)
        await wake_context_outboxes(context)
    rendered = tracker_task_payload(task, context.board, users)
    return rendered


async def delete_task(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    task_ref: EntityReferenceLike,
    if_match: str | None,
) -> None:
    proxied, _body = await proxy_remote_tracker_mutation(
        session,
        settings,
        auth,
        channel_ref,
        "tracker.task.delete",
        {"resource_ref": str(task_ref), "if_match": if_match},
        returns_body=False,
    )
    if proxied:
        return
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=True,
        needed=required_permissions("tracker.read"),
    )
    tasks = await ordered_tasks(session, context.board, lock=True)
    task_id, task_domain = task_ref.resolve(settings.domain)
    task = next(
        (item for item in tasks if (item.id, item.origin_domain) == (task_id, task_domain)), None
    )
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "TRACKER_TASK_NOT_FOUND"})
    require_tracker_version(task.updated_at, if_match)
    await require_task_edit(session, redis, context, auth.user, task)
    lane_tasks = [
        item
        for item in tasks
        if (item.lane_id, item.lane_domain) == (task.lane_id, task.lane_domain) and item is not task
    ]
    positions_shifted = other_positions_need_normalization(lane_tasks)
    mutation_version = next_tracker_version(context.board.updated_at)
    apply_positions(lane_tasks, updated_at=mutation_version)
    deleted: dict[str, object] = {
        "channel_id": str(task.channel_id),
        "channel_domain": task.channel_domain,
        "task_id": str(task.id),
        "task_domain": task.origin_domain,
    }
    context.board.updated_at = mutation_version
    await session.delete(task)
    deleted["board_version"] = context.board.updated_at.isoformat()
    await queue_context_federation(
        session,
        settings,
        context,
        auth.user,
        reason="task_order_updated" if positions_shifted else "task_deleted",
    )
    if positions_shifted:
        queue_board_update(session, context, reason="task_order_updated")
    queue_context_dispatch(session, context, "TRACKER_TASK_DELETE", deleted)
    await session.commit()
    await wake_context_outboxes(context)
