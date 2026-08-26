from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import (
    Channel,
    Guild,
    GuildMember,
    TrackerBoard,
    TrackerDispatchOutbox,
    TrackerLane,
    TrackerTask,
    User,
)
from app.federation.client import signed_request
from app.federation.network import decode_federation_response_json, normalize_domain
from app.federation.replica_storage import admit_replica_storage
from app.federation.replication import (
    database_snowflake,
    profile_from_user,
    resolve_delegated_profile,
)
from app.federation.schemas import RemoteUserProfile
from app.tracker.outbox import queue_tracker_dispatch

TRACKER_CHANNEL_TYPE = 17
MAX_TRACKER_LANES = 50
MAX_TRACKER_TASKS = 5_000
MAX_TRACKER_USERS = 10_000
MAX_TRACKER_SNAPSHOT_PAGES = 100
MAX_TRACKER_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_TRACKER_PAGE_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TRACKER_PAGE_TASK_CANDIDATES = 256
TARGET_TRACKER_PAGE_BYTES = 6 * 1024 * 1024
MAX_TRACKER_SYNC_SECONDS = 45.0

TRACKER_INVALIDATION_REASONS = frozenset(
    {
        "settings_updated",
        "lane_created",
        "lane_updated",
        "lane_completion_updated",
        "lane_order_updated",
        "lane_deleted",
        "task_created",
        "task_updated",
        "task_order_updated",
        "task_deleted",
        "assignee_membership_removed",
    }
)


def tracker_replica_lock_key(channel_id: int, channel_domain: str) -> str:
    return f"kaede-replicated-tracker:{channel_domain}:{channel_id}"


async def lock_replicated_tracker(
    session: AsyncSession,
    channel_id: int,
    channel_domain: str,
) -> None:
    """Serialize remote hydration with ordered invalidation application."""

    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    tracker_replica_lock_key(channel_id, channel_domain),
                    0,
                )
            )
        )
    )


async def apply_tracker_invalidation(
    session: AsyncSession,
    guild: Guild,
    content: dict[str, Any],
    context: dict[str, Any],
) -> None:
    """Replace a remote cache with a durable, explicitly stale sentinel."""

    raw = content.get("tracker")
    if not isinstance(raw, dict):
        raise ValueError("tracker invalidation payload is invalid")
    channel_ref = (
        database_snowflake(raw.get("channel_id"), "tracker channel id"),
        normalize_domain(str(raw.get("channel_domain", ""))),
    )
    context_ref = (
        database_snowflake(context.get("channel_id"), "tracker context channel id"),
        normalize_domain(str(context.get("channel_domain", ""))),
    )
    if channel_ref != context_ref or channel_ref[1] != guild.origin_domain:
        raise ValueError("tracker invalidation references another channel")
    channel = await session.get(Channel, channel_ref)
    if (
        channel is None
        or channel.type != TRACKER_CHANNEL_TYPE
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
    ):
        raise ValueError("tracker invalidation references an invalid channel")
    key_prefix = raw.get("key_prefix")
    if (
        not isinstance(key_prefix, str)
        or not 2 <= len(key_prefix) <= 10
        or not key_prefix.isascii()
        or not key_prefix.isalnum()
        or not key_prefix[0].isalpha()
        or key_prefix != key_prefix.upper()
    ):
        raise ValueError("tracker invalidation key prefix is invalid")
    next_task_number = database_snowflake(raw.get("next_task_number"), "tracker next task number")
    if next_task_number < 1:
        raise ValueError("tracker invalidation next task number is invalid")
    version = _aware_datetime(raw.get("version"), "tracker invalidation version")
    if version is None:
        raise ValueError("tracker invalidation version is invalid")
    reason = content.get("reason")
    if reason not in TRACKER_INVALIDATION_REASONS:
        raise ValueError("tracker invalidation reason is invalid")

    await lock_replicated_tracker(session, channel.id, channel.origin_domain)
    board = await session.get(TrackerBoard, channel_ref)
    # Lazy hydration can legitimately race ahead of ordered event delivery. An
    # older invalidation still wakes clients, but it must not delete a cache
    # fetched at that revision or a newer one.
    if board is not None and (board.guild_id, board.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise ValueError("tracker invalidation conflicts with another board")
    cache_is_current = board is not None and board.updated_at >= version
    if board is None:
        board = TrackerBoard(
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            key_prefix=key_prefix,
            next_task_number=next_task_number,
            created_at=version,
            updated_at=version,
        )
        session.add(board)
    elif not cache_is_current:
        board.key_prefix = key_prefix
        board.next_task_number = next_task_number
        board.updated_at = version
    if not cache_is_current:
        # Drop tasks before lanes because their normalized FK deliberately does
        # not cascade an individual lane deletion on authoritative boards.
        await session.execute(
            delete(TrackerTask).where(
                TrackerTask.channel_id == channel.id,
                TrackerTask.channel_domain == channel.origin_domain,
            )
        )
        await session.execute(
            delete(TrackerLane).where(
                TrackerLane.channel_id == channel.id,
                TrackerLane.channel_domain == channel.origin_domain,
            )
        )
    # Coalesce undelivered projections: a full-refresh invalidation subsumes
    # every older tracker event for this channel.
    await session.execute(
        delete(TrackerDispatchOutbox).where(
            TrackerDispatchOutbox.channel_id == channel.id,
            TrackerDispatchOutbox.channel_domain == channel.origin_domain,
        )
    )
    await session.flush()
    queue_tracker_dispatch(
        session,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        event_type="TRACKER_BOARD_UPDATE",
        payload={
            "channel_id": str(channel.id),
            "channel_domain": channel.origin_domain,
            "key_prefix": board.key_prefix,
            "next_task_number": str(board.next_task_number),
            "version": board.updated_at.isoformat(),
            "full_refresh": True,
            "reason": str(reason),
        },
    )


def _aware_datetime(raw: object, label: str, *, optional: bool = False) -> datetime | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{label} is invalid")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{label} is invalid") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} lacks a timezone")
    return value


def _tracker_ref(
    raw: object,
    label: str,
    *,
    id_field: str = "id",
    domain_field: str = "origin_domain",
) -> tuple[int, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"tracker {label} is invalid")
    return (
        database_snowflake(raw.get(id_field), f"tracker {label} id"),
        normalize_domain(str(raw.get(domain_field, ""))),
    )


def tracker_board_snapshot_payload(
    board: TrackerBoard,
    *,
    task_count: int,
) -> dict[str, object]:
    return {
        "channel_id": str(board.channel_id),
        "channel_domain": board.channel_domain,
        "guild_id": str(board.guild_id),
        "guild_domain": board.guild_domain,
        "key_prefix": board.key_prefix,
        "next_task_number": str(board.next_task_number),
        "task_count": str(task_count),
        "created_at": board.created_at.isoformat(),
        "updated_at": board.updated_at.isoformat(),
        "version": board.updated_at.isoformat(),
    }


def tracker_lane_snapshot_payload(lane: TrackerLane) -> dict[str, object]:
    return {
        "id": str(lane.id),
        "origin_domain": lane.origin_domain,
        "channel_id": str(lane.channel_id),
        "channel_domain": lane.channel_domain,
        "guild_id": str(lane.guild_id),
        "guild_domain": lane.guild_domain,
        "name": lane.name,
        "color": lane.color,
        "kind": lane.kind,
        "completed": lane.completed,
        "position": lane.position,
        "created_at": lane.created_at.isoformat(),
        "updated_at": lane.updated_at.isoformat(),
    }


def tracker_task_snapshot_payload(task: TrackerTask) -> dict[str, object]:
    return {
        "id": str(task.id),
        "origin_domain": task.origin_domain,
        "channel_id": str(task.channel_id),
        "channel_domain": task.channel_domain,
        "guild_id": str(task.guild_id),
        "guild_domain": task.guild_domain,
        "lane_id": str(task.lane_id),
        "lane_domain": task.lane_domain,
        "number": str(task.number),
        "title": task.title,
        "description": task.description,
        "priority": task.priority,
        "position": task.position,
        "due_at": task.due_at.isoformat() if task.due_at is not None else None,
        "completed_at": (task.completed_at.isoformat() if task.completed_at is not None else None),
        "creator_id": str(task.creator_id),
        "creator_domain": task.creator_domain,
        "assignee_id": str(task.assignee_id) if task.assignee_id is not None else None,
        "assignee_domain": task.assignee_domain,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def tracker_snapshot_cursor(
    settings: Settings,
    board: TrackerBoard,
    after_task_id: int,
) -> str:
    payload = json.dumps(
        {
            "guild_id": str(board.guild_id),
            "guild_domain": board.guild_domain,
            "channel_id": str(board.channel_id),
            "channel_domain": board.channel_domain,
            "version": board.updated_at.isoformat(),
            "after_task_id": str(after_task_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(settings.secret_key_bytes, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")


def tracker_snapshot_cursor_task_id(
    settings: Settings,
    board: TrackerBoard,
    cursor: str,
) -> int:
    if not 1 <= len(cursor) <= 1024 or not cursor.isascii():
        raise ValueError("tracker snapshot cursor is invalid")
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload, signature = decoded[:-32], decoded[-32:]
        expected = hmac.new(settings.secret_key_bytes, payload, hashlib.sha256).digest()
        raw = json.loads(payload)
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError("tracker snapshot cursor is invalid") from None
    if len(signature) != 32 or not hmac.compare_digest(signature, expected):
        raise ValueError("tracker snapshot cursor signature is invalid")
    if not isinstance(raw, dict):
        raise ValueError("tracker snapshot cursor is invalid")
    if (
        database_snowflake(raw.get("guild_id"), "tracker cursor guild id"),
        normalize_domain(str(raw.get("guild_domain", ""))),
        database_snowflake(raw.get("channel_id"), "tracker cursor channel id"),
        normalize_domain(str(raw.get("channel_domain", ""))),
        raw.get("version"),
    ) != (
        board.guild_id,
        board.guild_domain,
        board.channel_id,
        board.channel_domain,
        board.updated_at.isoformat(),
    ):
        raise TrackerSnapshotChanged
    return database_snowflake(raw.get("after_task_id"), "tracker cursor task id")


def tracker_snapshot_page_payload(
    settings: Settings,
    board: TrackerBoard,
    lanes: Sequence[TrackerLane],
    tasks: Sequence[TrackerTask],
    users: Sequence[User],
    *,
    task_count: int,
    has_more: bool,
) -> dict[str, object]:
    if has_more and not tasks:
        raise ValueError("tracker snapshot cannot advance an empty page")
    return {
        "board": tracker_board_snapshot_payload(board, task_count=task_count),
        "lanes": [tracker_lane_snapshot_payload(lane) for lane in lanes],
        "tasks": [tracker_task_snapshot_payload(task) for task in tasks],
        "users": [profile_from_user(user) for user in users],
        "assignee_member_refs": [
            {"id": str(user_id), "origin_domain": user_domain}
            for user_id, user_domain in sorted(
                {
                    (task.assignee_id, task.assignee_domain)
                    for task in tasks
                    if task.assignee_id is not None and task.assignee_domain is not None
                },
                key=lambda item: (item[1], item[0]),
            )
        ],
        "next_cursor": (
            tracker_snapshot_cursor(settings, board, tasks[-1].id) if has_more else None
        ),
        "next_cursor_after_task_id": str(tasks[-1].id) if has_more else None,
    }


def tracker_snapshot_page_size(
    tasks: Sequence[TrackerTask],
    users: dict[tuple[int, str], User],
) -> int:
    """Conservatively size variable page content before JSON rendering."""

    task_payloads = [tracker_task_snapshot_payload(task) for task in tasks]
    user_refs = {(task.creator_id, task.creator_domain) for task in tasks} | {
        (task.assignee_id, task.assignee_domain)
        for task in tasks
        if task.assignee_id is not None and task.assignee_domain is not None
    }
    user_payloads = [profile_from_user(users[ref]) for ref in user_refs]
    return len(
        json.dumps(
            {"tasks": task_payloads, "users": user_payloads},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def validate_tracker_snapshot(
    snapshot: dict[str, Any],
    *,
    expected_guild: tuple[int, str],
    expected_channel: tuple[int, str],
) -> None:
    raw_board = snapshot.get("board")
    lanes = snapshot.get("lanes")
    tasks = snapshot.get("tasks")
    users = snapshot.get("users")
    assignee_members = snapshot.get("assignee_member_refs")
    if (
        not isinstance(raw_board, dict)
        or not isinstance(lanes, list)
        or not isinstance(tasks, list)
        or not isinstance(users, list)
        or not isinstance(assignee_members, list)
    ):
        raise ValueError("tracker snapshot collections are invalid")
    if (
        len(lanes) > MAX_TRACKER_LANES
        or len(tasks) > MAX_TRACKER_TASKS
        or len(users) > MAX_TRACKER_USERS
        or len(assignee_members) > MAX_TRACKER_TASKS
    ):
        raise ValueError("tracker snapshot exceeds its protocol bound")

    board_ref = (
        database_snowflake(raw_board.get("channel_id"), "tracker board channel id"),
        normalize_domain(str(raw_board.get("channel_domain", ""))),
    )
    guild_ref = (
        database_snowflake(raw_board.get("guild_id"), "tracker board guild id"),
        normalize_domain(str(raw_board.get("guild_domain", ""))),
    )
    if board_ref != expected_channel or guild_ref != expected_guild:
        raise ValueError("tracker snapshot identifies another board")
    if board_ref[1] != guild_ref[1]:
        raise ValueError("tracker snapshot board origin is invalid")
    key_prefix = raw_board.get("key_prefix")
    if (
        not isinstance(key_prefix, str)
        or not 2 <= len(key_prefix) <= 10
        or not key_prefix.isascii()
        or not key_prefix.isalnum()
        or not key_prefix[0].isalpha()
        or key_prefix != key_prefix.upper()
    ):
        raise ValueError("tracker snapshot key prefix is invalid")
    next_task_number = database_snowflake(
        raw_board.get("next_task_number"), "tracker next task number"
    )
    if next_task_number < 1:
        raise ValueError("tracker snapshot next task number is invalid")
    task_count = database_snowflake(raw_board.get("task_count"), "tracker task count")
    if task_count != len(tasks) or task_count > MAX_TRACKER_TASKS:
        raise ValueError("tracker snapshot task count is invalid")
    created_at = _aware_datetime(raw_board.get("created_at"), "tracker board creation")
    updated_at = _aware_datetime(raw_board.get("updated_at"), "tracker board update")
    if created_at is None or updated_at is None or updated_at < created_at:
        raise ValueError("tracker snapshot board timestamps are invalid")
    if raw_board.get("version") != raw_board.get("updated_at"):
        raise ValueError("tracker snapshot board version is invalid")

    lane_refs: set[tuple[int, str]] = set()
    lane_positions: set[int] = set()
    for raw in lanes:
        lane_ref = _tracker_ref(raw, "lane")
        if lane_ref[1] != board_ref[1] or lane_ref in lane_refs:
            raise ValueError("tracker snapshot lane identity is invalid")
        if not isinstance(raw, dict):
            raise ValueError("tracker snapshot lane is invalid")
        if (
            database_snowflake(raw.get("channel_id"), "tracker lane channel id"),
            normalize_domain(str(raw.get("channel_domain", ""))),
        ) != board_ref or (
            database_snowflake(raw.get("guild_id"), "tracker lane guild id"),
            normalize_domain(str(raw.get("guild_domain", ""))),
        ) != guild_ref:
            raise ValueError("tracker snapshot lane references another board")
        name = raw.get("name")
        color = raw.get("color")
        position = raw.get("position")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("tracker snapshot lane name is invalid")
        if isinstance(color, bool) or not isinstance(color, int) or not 0 <= color <= 0xFFFFFF:
            raise ValueError("tracker snapshot lane color is invalid")
        if raw.get("kind") not in {
            "backlog",
            "planned",
            "in_progress",
            "completed",
            "custom",
        } or not isinstance(raw.get("completed"), bool):
            raise ValueError("tracker snapshot lane state is invalid")
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position < MAX_TRACKER_LANES
            or position in lane_positions
        ):
            raise ValueError("tracker snapshot lane position is invalid")
        lane_created = _aware_datetime(raw.get("created_at"), "tracker lane creation")
        lane_updated = _aware_datetime(raw.get("updated_at"), "tracker lane update")
        if lane_created is None or lane_updated is None or lane_updated < lane_created:
            raise ValueError("tracker snapshot lane timestamps are invalid")
        lane_refs.add(lane_ref)
        lane_positions.add(position)
    if not lane_refs or lane_positions != set(range(len(lane_refs))):
        raise ValueError("tracker snapshot lane ordering is invalid")

    user_profiles: dict[tuple[int, str], RemoteUserProfile] = {}
    for raw in users:
        profile = RemoteUserProfile.model_validate(raw)
        ref = (int(profile.id), profile.origin_domain)
        if ref in user_profiles:
            raise ValueError("tracker snapshot contains a duplicate user profile")
        user_profiles[ref] = profile
    assignee_refs: set[tuple[int, str]] = set()
    for raw in assignee_members:
        ref = _tracker_ref(raw, "assignee member")
        if ref in assignee_refs:
            raise ValueError("tracker snapshot contains a duplicate assignee member")
        assignee_refs.add(ref)

    task_refs: set[tuple[int, str]] = set()
    task_numbers: set[int] = set()
    task_positions: dict[tuple[int, str], set[int]] = {lane_ref: set() for lane_ref in lane_refs}
    referenced_users: set[tuple[int, str]] = set()
    referenced_assignees: set[tuple[int, str]] = set()
    maximum_number = 0
    for raw in tasks:
        task_ref = _tracker_ref(raw, "task")
        if task_ref[1] != board_ref[1] or task_ref in task_refs:
            raise ValueError("tracker snapshot task identity is invalid")
        if not isinstance(raw, dict):
            raise ValueError("tracker snapshot task is invalid")
        if (
            database_snowflake(raw.get("channel_id"), "tracker task channel id"),
            normalize_domain(str(raw.get("channel_domain", ""))),
        ) != board_ref or (
            database_snowflake(raw.get("guild_id"), "tracker task guild id"),
            normalize_domain(str(raw.get("guild_domain", ""))),
        ) != guild_ref:
            raise ValueError("tracker snapshot task references another board")
        lane_ref = (
            database_snowflake(raw.get("lane_id"), "tracker task lane id"),
            normalize_domain(str(raw.get("lane_domain", ""))),
        )
        if lane_ref not in lane_refs:
            raise ValueError("tracker snapshot task references an unknown lane")
        number = database_snowflake(raw.get("number"), "tracker task number")
        title = raw.get("title")
        description = raw.get("description")
        position = raw.get("position")
        if number < 1 or number in task_numbers:
            raise ValueError("tracker snapshot task number is invalid")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError("tracker snapshot task title is invalid")
        if description is not None and (
            not isinstance(description, str) or len(description) > 10_000
        ):
            raise ValueError("tracker snapshot task description is invalid")
        if raw.get("priority") not in {"none", "low", "medium", "high", "urgent"}:
            raise ValueError("tracker snapshot task priority is invalid")
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position < MAX_TRACKER_TASKS
            or position in task_positions[lane_ref]
        ):
            raise ValueError("tracker snapshot task position is invalid")
        _aware_datetime(raw.get("due_at"), "tracker task due time", optional=True)
        _aware_datetime(raw.get("completed_at"), "tracker task completion", optional=True)
        task_created = _aware_datetime(raw.get("created_at"), "tracker task creation")
        task_updated = _aware_datetime(raw.get("updated_at"), "tracker task update")
        if task_created is None or task_updated is None or task_updated < task_created:
            raise ValueError("tracker snapshot task timestamps are invalid")
        creator_ref = (
            database_snowflake(raw.get("creator_id"), "tracker creator id"),
            normalize_domain(str(raw.get("creator_domain", ""))),
        )
        raw_assignee_id = raw.get("assignee_id")
        raw_assignee_domain = raw.get("assignee_domain")
        if (raw_assignee_id is None) != (raw_assignee_domain is None):
            raise ValueError("tracker snapshot assignee identity is incomplete")
        if raw_assignee_id is not None:
            assignee_ref = (
                database_snowflake(raw_assignee_id, "tracker assignee id"),
                normalize_domain(str(raw_assignee_domain)),
            )
            referenced_assignees.add(assignee_ref)
            referenced_users.add(assignee_ref)
        referenced_users.add(creator_ref)
        task_refs.add(task_ref)
        task_numbers.add(number)
        task_positions[lane_ref].add(position)
        maximum_number = max(maximum_number, number)
    if next_task_number <= maximum_number:
        raise ValueError("tracker snapshot next task number does not advance task keys")
    if any(positions != set(range(len(positions))) for positions in task_positions.values()):
        raise ValueError("tracker snapshot task ordering is invalid")
    if set(user_profiles) != referenced_users:
        raise ValueError("tracker snapshot user profiles do not match task references")
    if assignee_refs != referenced_assignees:
        raise ValueError("tracker snapshot assignee memberships do not match tasks")


async def _fetch_tracker_snapshot_once(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    channel: Channel,
    *,
    deadline: float,
) -> dict[str, Any]:
    combined: dict[str, Any] | None = None
    cursor: str | None = None
    total_bytes = 0
    last_task_id = -1
    profiles: dict[tuple[int, str], dict[str, Any]] = {}
    assignee_refs: set[tuple[int, str]] = set()
    for _page in range(MAX_TRACKER_SNAPSHOT_PAGES):
        remaining_time = deadline - time.monotonic()
        remaining_bytes = MAX_TRACKER_SNAPSHOT_BYTES - total_bytes
        if remaining_time <= 0:
            raise RuntimeError("tracker snapshot exceeded its duration limit")
        if remaining_bytes <= 0:
            raise RuntimeError("tracker snapshot exceeded its aggregate byte limit")
        response = await signed_request(
            session,
            settings,
            "GET",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/trackers/{channel.id}/snapshot",
            query={"cursor": cursor} if cursor is not None else {},
            request_timeout=min(10.0, remaining_time),
            max_response_bytes=min(MAX_TRACKER_PAGE_RESPONSE_BYTES, remaining_bytes),
        )
        if response.status_code == 409:
            raise TrackerSnapshotChanged
        if response.status_code == 403:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail={"code": "TRACKER_NOT_FOUND"})
        if response.status_code != 200:
            raise RuntimeError("tracker snapshot fetch failed")
        total_bytes += len(response.content)
        payload = decode_federation_response_json(response)
        if not isinstance(payload, dict):
            raise RuntimeError("tracker snapshot page is invalid")
        raw_board = payload.get("board")
        lanes = payload.get("lanes")
        tasks = payload.get("tasks")
        users = payload.get("users")
        page_assignees = payload.get("assignee_member_refs")
        if (
            not isinstance(raw_board, dict)
            or not isinstance(lanes, list)
            or not isinstance(tasks, list)
            or not isinstance(users, list)
            or not isinstance(page_assignees, list)
            or len(tasks) > MAX_TRACKER_PAGE_TASK_CANDIDATES
        ):
            raise RuntimeError("tracker snapshot page collections are invalid")
        if combined is None:
            combined = {
                "board": raw_board,
                "lanes": lanes,
                "tasks": [],
                "users": [],
                "assignee_member_refs": [],
            }
        elif raw_board != combined["board"] or lanes != combined["lanes"]:
            raise TrackerSnapshotChanged
        for raw_task in tasks:
            task_ref = _tracker_ref(raw_task, "page task")
            if task_ref[1] != channel.origin_domain or task_ref[0] <= last_task_id:
                raise RuntimeError("tracker snapshot task cursor did not advance")
            last_task_id = task_ref[0]
            combined["tasks"].append(raw_task)
        for raw_user in users:
            profile = RemoteUserProfile.model_validate(raw_user)
            ref = (int(profile.id), profile.origin_domain)
            previous = profiles.get(ref)
            normalized = profile.model_dump(mode="json")
            if previous is not None and previous != normalized:
                raise TrackerSnapshotChanged
            profiles[ref] = normalized
        for raw_member in page_assignees:
            assignee_refs.add(_tracker_ref(raw_member, "page assignee member"))
        next_cursor = payload.get("next_cursor")
        next_after_task_id = payload.get("next_cursor_after_task_id")
        if next_cursor is None:
            if next_after_task_id is not None:
                raise RuntimeError("tracker snapshot final cursor is invalid")
            expected_count = database_snowflake(
                raw_board.get("task_count"), "tracker snapshot task count"
            )
            if expected_count != len(combined["tasks"]):
                raise RuntimeError("tracker snapshot ended before its declared task count")
            combined["users"] = [
                profiles[ref] for ref in sorted(profiles, key=lambda item: (item[1], item[0]))
            ]
            combined["assignee_member_refs"] = [
                {"id": str(ref[0]), "origin_domain": ref[1]}
                for ref in sorted(assignee_refs, key=lambda x: (x[1], x[0]))
            ]
            validate_tracker_snapshot(
                combined,
                expected_guild=(guild.id, guild.origin_domain),
                expected_channel=(channel.id, channel.origin_domain),
            )
            return combined
        if (
            not isinstance(next_cursor, str)
            or not tasks
            or len(next_cursor) > 1024
            or database_snowflake(
                next_after_task_id,
                "tracker snapshot cursor task id",
            )
            != last_task_id
        ):
            raise RuntimeError("tracker snapshot returned an invalid cursor")
        cursor = next_cursor
    raise RuntimeError("tracker snapshot exceeded its page limit")


class TrackerSnapshotChanged(RuntimeError):
    pass


async def fetch_tracker_snapshot(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    channel: Channel,
) -> dict[str, Any]:
    deadline = time.monotonic() + MAX_TRACKER_SYNC_SECONDS
    for attempt in range(3):
        try:
            return await _fetch_tracker_snapshot_once(
                session,
                settings,
                guild,
                channel,
                deadline=deadline,
            )
        except TrackerSnapshotChanged:
            if attempt == 2 or time.monotonic() >= deadline:
                break
    raise HTTPException(status_code=503, detail={"code": "FEDERATION_UNAVAILABLE"})


async def apply_tracker_snapshot(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    channel: Channel,
    snapshot: dict[str, Any],
) -> TrackerBoard:
    validate_tracker_snapshot(
        snapshot,
        expected_guild=(guild.id, guild.origin_domain),
        expected_channel=(channel.id, channel.origin_domain),
    )
    profiles = {
        (int(profile.id), profile.origin_domain): await resolve_delegated_profile(
            session,
            settings,
            profile,
            authority_origin=guild.origin_domain,
        )
        for profile in (RemoteUserProfile.model_validate(raw) for raw in snapshot["users"])
    }
    assignee_refs = {
        _tracker_ref(raw, "assignee member") for raw in snapshot["assignee_member_refs"]
    }
    if assignee_refs:
        local_members = set(
            (
                await session.execute(
                    select(GuildMember.user_id, GuildMember.user_domain).where(
                        GuildMember.guild_id == guild.id,
                        GuildMember.guild_domain == guild.origin_domain,
                        tuple_(GuildMember.user_id, GuildMember.user_domain).in_(assignee_refs),
                    )
                )
            ).tuples()
        )
        # Local membership-intent filtering can deliberately hide a local user
        # that the authority has not removed yet. Keep the authoritative task,
        # but do not recreate an assignment to a locally departed principal.
        applicable_assignees = assignee_refs & local_members
    else:
        applicable_assignees = set()

    raw_board = snapshot["board"]
    board = await session.get(TrackerBoard, (channel.id, channel.origin_domain))
    if board is None:
        board = TrackerBoard(
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            key_prefix=str(raw_board["key_prefix"]),
            next_task_number=int(raw_board["next_task_number"]),
        )
        session.add(board)
    board.key_prefix = str(raw_board["key_prefix"])
    board.next_task_number = int(raw_board["next_task_number"])
    board.created_at = datetime.fromisoformat(str(raw_board["created_at"]))
    board.updated_at = datetime.fromisoformat(str(raw_board["updated_at"]))
    await session.execute(
        delete(TrackerTask).where(
            TrackerTask.channel_id == channel.id,
            TrackerTask.channel_domain == channel.origin_domain,
        )
    )
    await session.execute(
        delete(TrackerLane).where(
            TrackerLane.channel_id == channel.id,
            TrackerLane.channel_domain == channel.origin_domain,
        )
    )
    await session.flush()
    for raw in snapshot["lanes"]:
        session.add(
            TrackerLane(
                id=int(raw["id"]),
                origin_domain=str(raw["origin_domain"]),
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                name=str(raw["name"]),
                color=int(raw["color"]),
                kind=str(raw["kind"]),
                completed=bool(raw["completed"]),
                position=int(raw["position"]),
                created_at=datetime.fromisoformat(str(raw["created_at"])),
                updated_at=datetime.fromisoformat(str(raw["updated_at"])),
            )
        )
    await session.flush()
    for raw in snapshot["tasks"]:
        creator_ref = (int(raw["creator_id"]), str(raw["creator_domain"]))
        if creator_ref not in profiles:
            raise RuntimeError("validated tracker creator profile disappeared")
        assignee_ref = (
            (int(raw["assignee_id"]), str(raw["assignee_domain"]))
            if raw.get("assignee_id") is not None
            else None
        )
        session.add(
            TrackerTask(
                id=int(raw["id"]),
                origin_domain=str(raw["origin_domain"]),
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                lane_id=int(raw["lane_id"]),
                lane_domain=str(raw["lane_domain"]),
                number=int(raw["number"]),
                title=str(raw["title"]),
                description=raw.get("description"),
                priority=str(raw["priority"]),
                position=int(raw["position"]),
                due_at=(
                    datetime.fromisoformat(str(raw["due_at"]))
                    if raw.get("due_at") is not None
                    else None
                ),
                completed_at=(
                    datetime.fromisoformat(str(raw["completed_at"]))
                    if raw.get("completed_at") is not None
                    else None
                ),
                creator_id=creator_ref[0],
                creator_domain=creator_ref[1],
                assignee_id=(
                    assignee_ref[0]
                    if assignee_ref is not None and assignee_ref in applicable_assignees
                    else None
                ),
                assignee_domain=(
                    assignee_ref[1]
                    if assignee_ref is not None and assignee_ref in applicable_assignees
                    else None
                ),
                client_nonce=None,
                client_request_hash=None,
                created_at=datetime.fromisoformat(str(raw["created_at"])),
                updated_at=datetime.fromisoformat(str(raw["updated_at"])),
            )
        )
    await admit_replica_storage(session, settings, guild)
    return board


async def replicated_tracker_is_stale(
    session: AsyncSession,
    guild: Guild,
    board: TrackerBoard | None,
) -> bool:
    if board is None:
        return True
    return (
        await session.scalar(
            select(TrackerLane.id)
            .where(
                TrackerLane.channel_id == board.channel_id,
                TrackerLane.channel_domain == board.channel_domain,
            )
            .limit(1)
        )
        is None
    )


async def hydrate_replicated_tracker(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    channel: Channel,
) -> TrackerBoard:
    """Lazily load a readable remote tracker after local permission checks."""

    del redis  # Reserved for future observability/cache coalescing; SQL is authoritative.
    if guild.origin_domain == settings.domain or channel.origin_domain != guild.origin_domain:
        raise ValueError("tracker hydration requires a remote authoritative channel")
    if channel.type != TRACKER_CHANNEL_TYPE or (channel.guild_id, channel.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "TRACKER_NOT_FOUND"})
    await lock_replicated_tracker(session, channel.id, channel.origin_domain)
    board = await session.get(
        TrackerBoard,
        (channel.id, channel.origin_domain),
        populate_existing=True,
    )
    if not await replicated_tracker_is_stale(session, guild, board):
        if board is None:
            raise RuntimeError("tracker cache state changed unexpectedly")
        return board
    try:
        snapshot = await fetch_tracker_snapshot(session, settings, guild, channel)
        board = await apply_tracker_snapshot(session, settings, guild, channel, snapshot)
        await session.commit()
    except HTTPException:
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATION_UNAVAILABLE"},
        ) from exc
    # The cache transaction is durable now, but committing released the
    # transaction-scoped advisory lock. Reacquire it before rendering so an
    # ordered invalidation cannot clear the freshly hydrated lanes between
    # this return and ``board_response`` in the caller.
    await lock_replicated_tracker(session, channel.id, channel.origin_domain)
    loaded = await session.get(
        TrackerBoard,
        (channel.id, channel.origin_domain),
        populate_existing=True,
    )
    if loaded is None:
        raise RuntimeError("hydrated tracker board disappeared")
    return loaded
