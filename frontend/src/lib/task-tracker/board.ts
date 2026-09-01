import { entityKey } from '$lib/chat/refs';
import { Permission } from '$lib/generated/permissions';
import type {
  TrackerBoard,
  TrackerFilters,
  TrackerGatewayDispatch,
  TrackerLane,
  TrackerTask
} from './types';

export const TrackerPermission = {
  ADMINISTRATOR: Permission.ADMINISTRATOR,
  CREATE_TASKS: Permission.CREATE_TRACKER_TASKS,
  EDIT_OWN_TASKS: Permission.EDIT_OWN_TRACKER_TASKS,
  MANAGE_TASKS: Permission.MANAGE_TRACKER_TASKS,
  ASSIGN_TASKS: Permission.ASSIGN_TRACKER_TASKS,
  MANAGE_TRACKER: Permission.MANAGE_TRACKER
} as const;

const TRACKER_EVENTS = new Set([
  'TRACKER_BOARD_UPDATE',
  'TRACKER_LANE_CREATE',
  'TRACKER_LANE_UPDATE',
  'TRACKER_LANE_DELETE',
  'TRACKER_TASK_CREATE',
  'TRACKER_TASK_UPDATE',
  'TRACKER_TASK_DELETE',
  'CHANNEL_PERMISSION_UPDATE'
]);

export function compareTrackerLanes(left: TrackerLane, right: TrackerLane): number {
  return left.position - right.position || entityKey(left).localeCompare(entityKey(right));
}

export function compareTrackerTasks(left: TrackerTask, right: TrackerTask): number {
  const position = left.position - right.position;
  if (position) return position;
  if (/^\d+$/.test(left.number) && /^\d+$/.test(right.number)) {
    const leftNumber = BigInt(left.number);
    const rightNumber = BigInt(right.number);
    if (leftNumber < rightNumber) return -1;
    if (leftNumber > rightNumber) return 1;
  }
  return entityKey(left).localeCompare(entityKey(right));
}

export function trackerHasPermission(
  board: Pick<TrackerBoard, 'permissions'> | null,
  permission: bigint
): boolean {
  try {
    const effective = BigInt(board?.permissions ?? '0');
    return Boolean(effective & (TrackerPermission.ADMINISTRATOR | permission));
  } catch {
    return false;
  }
}

export function trackerTaskBelongsToUser(
  task: TrackerTask,
  user: { id: string; origin_domain: string } | null
): boolean {
  if (!user) return false;
  const key = entityKey(user);
  return (
    entityKey(task.creator) === key || Boolean(task.assignee && entityKey(task.assignee) === key)
  );
}

export type TrackerTaskEditMode = 'details' | 'assignment' | 'read-only';

export function trackerCanChangeAssignee(
  task: Pick<TrackerTask, 'assignee'> | null,
  user: { id: string; origin_domain: string } | null,
  canAssignOthers: boolean
): boolean {
  return (
    canAssignOthers ||
    Boolean(user && (!task?.assignee || entityKey(task.assignee) === entityKey(user)))
  );
}

/**
 * Assignment is an independent moderation capability. It must not implicitly
 * unlock the task's other fields, but it still needs a saveable editor.
 */
export function trackerTaskEditMode(
  canEditDetails: boolean,
  canAssign: boolean
): TrackerTaskEditMode {
  if (canEditDetails) return 'details';
  return canAssign ? 'assignment' : 'read-only';
}

export function orderedTrackerLanes(board: TrackerBoard | null): TrackerLane[] {
  return [...(board?.lanes ?? [])].sort(compareTrackerLanes);
}

export function trackerTasksForLane(board: TrackerBoard | null, lane: TrackerLane): TrackerTask[] {
  return (board?.tasks ?? [])
    .filter((task) => task.lane_id === lane.id && task.lane_domain === lane.origin_domain)
    .sort(compareTrackerTasks);
}

/** Converts a visual drop boundary into the server's insertion index after removing the task. */
export function trackerDropPosition(
  task: TrackerTask,
  destinationLane: TrackerLane,
  destinationTasks: TrackerTask[],
  boundary: number
): number | null {
  const bounded = Math.max(0, Math.min(boundary, destinationTasks.length));
  if (task.lane_id !== destinationLane.id || task.lane_domain !== destinationLane.origin_domain)
    return bounded;
  const sourceIndex = destinationTasks.findIndex(
    (candidate) => entityKey(candidate) === entityKey(task)
  );
  if (sourceIndex < 0) return Math.min(bounded, Math.max(0, destinationTasks.length - 1));
  const position = bounded > sourceIndex ? bounded - 1 : bounded;
  return position === sourceIndex ? null : position;
}

export function filterTrackerTasks(tasks: TrackerTask[], filters: TrackerFilters): TrackerTask[] {
  const query = filters.query.trim().toLocaleLowerCase();
  return tasks.filter((task) => {
    if (filters.priority !== 'all' && task.priority !== filters.priority) return false;
    if (
      filters.assignee &&
      entityKey(task.assignee ?? { id: '', origin_domain: '' }) !== filters.assignee
    )
      return false;
    if (!query) return true;
    return `${task.key} ${task.title} ${task.description ?? ''} ${task.assignee?.display_name ?? ''} ${task.assignee?.username ?? ''}`
      .toLocaleLowerCase()
      .includes(query);
  });
}

export function moveTaskInBoard(
  board: TrackerBoard,
  taskKey: string,
  destinationLane: TrackerLane,
  destinationIndex: number
): TrackerBoard {
  const task = board.tasks.find((candidate) => entityKey(candidate) === taskKey);
  if (!task) return board;
  const destination = board.tasks
    .filter(
      (candidate) =>
        entityKey(candidate) !== taskKey &&
        candidate.lane_id === destinationLane.id &&
        candidate.lane_domain === destinationLane.origin_domain
    )
    .sort(compareTrackerTasks);
  const boundedIndex = Math.max(0, Math.min(destinationIndex, destination.length));
  destination.splice(boundedIndex, 0, {
    ...task,
    lane_id: destinationLane.id,
    lane_domain: destinationLane.origin_domain
  });
  const destinationKeys = new Set(destination.map(entityKey));
  const tasks = board.tasks
    .filter((candidate) => !destinationKeys.has(entityKey(candidate)))
    .map((candidate) => candidate);
  tasks.push(...destination.map((candidate, position) => ({ ...candidate, position })));
  return {
    ...board,
    tasks,
    lanes: board.lanes.map((lane) => ({
      ...lane,
      task_count: tasks.filter(
        (candidate) => candidate.lane_id === lane.id && candidate.lane_domain === lane.origin_domain
      ).length
    }))
  };
}

export function trackerColor(color: number): string {
  const safe = Number.isInteger(color) ? Math.max(0, Math.min(color, 0xffffff)) : 0x64748b;
  return `#${safe.toString(16).padStart(6, '0')}`;
}

export function trackerDispatchTargetsChannel(
  dispatch: TrackerGatewayDispatch,
  channelId: string,
  channelDomain: string
): boolean {
  if (!TRACKER_EVENTS.has(dispatch.t) || !dispatch.d || typeof dispatch.d !== 'object')
    return false;
  const data = dispatch.d as Record<string, unknown>;
  const payload =
    data.board && typeof data.board === 'object'
      ? (data.board as Record<string, unknown>)
      : data.lane && typeof data.lane === 'object'
        ? (data.lane as Record<string, unknown>)
        : data.task && typeof data.task === 'object'
          ? (data.task as Record<string, unknown>)
          : data;
  return (
    String(payload.channel_id ?? data.channel_id ?? '') === channelId &&
    String(payload.channel_domain ?? data.channel_domain ?? '') === channelDomain
  );
}

/**
 * A fresh READY is a full gateway re-identify and may follow a replay gap. It
 * must reconcile open tracker projections even though READY is not scoped to a
 * channel. RESUMED retains the replayed sequence and needs no extra fetch.
 */
export function trackerDispatchRequiresRefresh(
  dispatch: TrackerGatewayDispatch,
  channelId: string,
  channelDomain: string
): boolean {
  return (
    dispatch.t === 'READY' || trackerDispatchTargetsChannel(dispatch, channelId, channelDomain)
  );
}
