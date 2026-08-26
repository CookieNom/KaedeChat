from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.chat.payloads import resource_version, user_payload
from app.db.models import TrackerBoard, TrackerLane, TrackerTask, User

UserKey = tuple[int, str]


def tracker_lane_payload(lane: TrackerLane, *, task_count: int) -> dict[str, object]:
    return {
        "id": str(lane.id),
        "origin_domain": lane.origin_domain,
        "channel_id": str(lane.channel_id),
        "channel_domain": lane.channel_domain,
        "name": lane.name,
        "color": lane.color,
        "kind": lane.kind,
        "completed": lane.completed,
        "position": lane.position,
        "task_count": task_count,
        "version": resource_version(lane),
    }


def tracker_task_payload(
    task: TrackerTask,
    board: TrackerBoard,
    users: Mapping[UserKey, User],
) -> dict[str, object]:
    creator = users.get((task.creator_id, task.creator_domain))
    if creator is None:
        raise RuntimeError("tracker task creator disappeared")
    assignee = (
        users.get((task.assignee_id, task.assignee_domain))
        if task.assignee_id is not None and task.assignee_domain is not None
        else None
    )
    if task.assignee_id is not None and assignee is None:
        raise RuntimeError("tracker task assignee disappeared")
    return {
        "id": str(task.id),
        "origin_domain": task.origin_domain,
        "channel_id": str(task.channel_id),
        "channel_domain": task.channel_domain,
        "lane_id": str(task.lane_id),
        "lane_domain": task.lane_domain,
        "number": str(task.number),
        "key": f"{board.key_prefix}-{task.number}",
        "title": task.title,
        "description": task.description,
        "priority": task.priority,
        "position": task.position,
        "due_at": task.due_at.isoformat() if task.due_at is not None else None,
        "completed_at": (task.completed_at.isoformat() if task.completed_at is not None else None),
        "creator": user_payload(creator),
        "assignee": user_payload(assignee) if assignee is not None else None,
        "version": resource_version(task),
    }


def tracker_board_payload(
    board: TrackerBoard,
    lanes: Sequence[TrackerLane],
    tasks: Sequence[TrackerTask],
    users: Mapping[UserKey, User],
    *,
    permissions: int,
) -> dict[str, object]:
    task_counts: dict[tuple[int, str], int] = {}
    for task in tasks:
        key = (task.lane_id, task.lane_domain)
        task_counts[key] = task_counts.get(key, 0) + 1
    return {
        "channel_id": str(board.channel_id),
        "channel_domain": board.channel_domain,
        "key_prefix": board.key_prefix,
        "next_task_number": str(board.next_task_number),
        "version": resource_version(board),
        "permissions": str(permissions),
        "lanes": [
            tracker_lane_payload(
                lane,
                task_count=task_counts.get((lane.id, lane.origin_domain), 0),
            )
            for lane in lanes
        ],
        "tasks": [tracker_task_payload(task, board, users) for task in tasks],
    }


def tracker_lane_event_payload(
    lane: TrackerLane,
    board: TrackerBoard,
    *,
    task_count: int,
) -> dict[str, object]:
    return {
        "channel_id": str(lane.channel_id),
        "channel_domain": lane.channel_domain,
        "board_version": resource_version(board),
        "lane": tracker_lane_payload(lane, task_count=task_count),
    }


def tracker_task_event_payload(
    task: TrackerTask,
    board: TrackerBoard,
    users: Mapping[UserKey, User],
) -> dict[str, object]:
    return {
        "channel_id": str(task.channel_id),
        "channel_domain": task.channel_domain,
        "board_version": resource_version(board),
        "task": tracker_task_payload(task, board, users),
    }
