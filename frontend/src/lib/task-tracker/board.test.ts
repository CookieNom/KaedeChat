import { describe, expect, it } from 'vitest';
import type { TrackerBoard, TrackerLane, TrackerTask } from './types';
import {
  filterTrackerTasks,
  moveTaskInBoard,
  orderedTrackerLanes,
  TrackerPermission,
  trackerHasPermission,
  trackerTaskBelongsToUser,
  trackerColor,
  trackerDispatchRequiresRefresh,
  trackerDispatchTargetsChannel,
  trackerDropPosition,
  trackerTasksForLane
} from './board';

const domain = 'chat.example';

function lane(id: string, position: number): TrackerLane {
  return {
    id,
    origin_domain: domain,
    channel_id: '10',
    channel_domain: domain,
    name: `Lane ${id}`,
    color: 0x22c55e,
    kind: 'custom',
    completed: false,
    position,
    task_count: 0,
    version: '1'
  };
}

function task(id: string, target: TrackerLane, position: number): TrackerTask {
  return {
    id,
    origin_domain: domain,
    channel_id: '10',
    channel_domain: domain,
    lane_id: target.id,
    lane_domain: target.origin_domain,
    number: id,
    key: `LOU-${id}`,
    title: `Task ${id}`,
    description: id === '2' ? 'Calendar integration' : null,
    priority: id === '1' ? 'high' : 'medium',
    position,
    due_at: null,
    completed_at: null,
    creator: {
      id: '50',
      origin_domain: domain,
      username: 'creator',
      display_name: 'Creator',
      avatar_hash: null,
      handle: `creator@${domain}`
    },
    assignee:
      id === '2'
        ? {
            id: '60',
            origin_domain: domain,
            username: 'mio',
            display_name: 'Mio',
            avatar_hash: null,
            handle: `mio@${domain}`
          }
        : null,
    version: '1'
  };
}

function board(): TrackerBoard {
  const backlog = lane('20', 1);
  const planned = lane('30', 0);
  return {
    channel_id: '10',
    channel_domain: domain,
    key_prefix: 'LOU',
    next_task_number: '3',
    version: '1',
    permissions: '0',
    lanes: [backlog, planned],
    tasks: [task('2', backlog, 0), task('1', planned, 0)]
  };
}

describe('task tracker board helpers', () => {
  it('orders lanes and tasks by stable positions', () => {
    const value = board();
    expect(orderedTrackerLanes(value).map((item) => item.id)).toEqual(['30', '20']);
    expect(trackerTasksForLane(value, value.lanes[0]).map((item) => item.id)).toEqual(['2']);
  });

  it('filters by searchable task and assignee fields', () => {
    const value = board();
    expect(
      filterTrackerTasks(value.tasks, {
        query: 'calendar',
        priority: 'all',
        assignee: '',
        hideCompleted: false
      }).map((item) => item.id)
    ).toEqual(['2']);
    expect(
      filterTrackerTasks(value.tasks, {
        query: 'mio',
        priority: 'medium',
        assignee: '60@chat.example',
        hideCompleted: false
      }).map((item) => item.id)
    ).toEqual(['2']);
  });

  it('optimistically moves a task and normalizes destination positions and counts', () => {
    const value = board();
    const destination = value.lanes[0];
    const moved = moveTaskInBoard(value, '1@chat.example', destination, 0);
    expect(trackerTasksForLane(moved, destination).map((item) => item.id)).toEqual(['1', '2']);
    expect(trackerTasksForLane(moved, destination).map((item) => item.position)).toEqual([0, 1]);
    expect(moved.lanes.find((item) => item.id === destination.id)?.task_count).toBe(2);
  });

  it('normalizes same-lane drag boundaries after removing the dragged task', () => {
    const value = board();
    const planned = value.lanes[1];
    const first = value.tasks[1];
    const second = { ...task('3', planned, 1), id: '3', number: '3' };
    const plannedTasks = [first, second];
    expect(trackerDropPosition(first, planned, plannedTasks, 1)).toBeNull();
    expect(trackerDropPosition(first, planned, plannedTasks, 2)).toBe(1);
    expect(trackerDropPosition(first, value.lanes[0], [value.tasks[0]], 1)).toBe(1);
  });

  it('matches tracker gateway envelopes to the open composite channel ref', () => {
    expect(
      trackerDispatchTargetsChannel(
        {
          t: 'TRACKER_TASK_UPDATE',
          d: { task: { channel_id: '10', channel_domain: domain } }
        },
        '10',
        domain
      )
    ).toBe(true);
    expect(
      trackerDispatchTargetsChannel(
        {
          t: 'CHANNEL_PERMISSION_UPDATE',
          d: { channel_id: '10', channel_domain: domain, permissions: '1024' }
        },
        '10',
        domain
      )
    ).toBe(true);
    expect(
      trackerDispatchTargetsChannel(
        { t: 'MESSAGE_CREATE', d: { channel_id: '10', channel_domain: domain } },
        '10',
        domain
      )
    ).toBe(false);
  });

  it('refreshes an open board after a fresh gateway session but not a resumed one', () => {
    expect(
      trackerDispatchRequiresRefresh({ t: 'READY', d: { session_id: 'fresh' } }, '10', domain)
    ).toBe(true);
    expect(trackerDispatchRequiresRefresh({ t: 'RESUMED', d: {} }, '10', domain)).toBe(false);
    expect(
      trackerDispatchRequiresRefresh(
        {
          t: 'TRACKER_TASK_UPDATE',
          d: { task: { channel_id: '10', channel_domain: domain } }
        },
        '10',
        domain
      )
    ).toBe(true);
  });

  it('formats server colors as bounded CSS values', () => {
    expect(trackerColor(0x00aaff)).toBe('#00aaff');
    expect(trackerColor(Number.NaN)).toBe('#64748b');
    expect(trackerColor(0xffffff + 1)).toBe('#ffffff');
  });

  it('fails closed on malformed permission masks and honors administrator access', () => {
    const value = board();
    expect(trackerHasPermission(value, TrackerPermission.CREATE_TASKS)).toBe(false);
    expect(
      trackerHasPermission(
        { ...value, permissions: TrackerPermission.CREATE_TASKS.toString() },
        TrackerPermission.CREATE_TASKS
      )
    ).toBe(true);
    expect(
      trackerHasPermission(
        { ...value, permissions: TrackerPermission.ADMINISTRATOR.toString() },
        TrackerPermission.MANAGE_TRACKER
      )
    ).toBe(true);
    expect(trackerHasPermission({ ...value, permissions: 'invalid' }, 1n)).toBe(false);
  });

  it('treats both creators and assignees as owners for own-task permissions', () => {
    const value = board();
    expect(trackerTaskBelongsToUser(value.tasks[0], { id: '60', origin_domain: domain })).toBe(
      true
    );
    expect(trackerTaskBelongsToUser(value.tasks[0], { id: '50', origin_domain: domain })).toBe(
      true
    );
    expect(
      trackerTaskBelongsToUser(value.tasks[0], { id: '50', origin_domain: 'other.example' })
    ).toBe(false);
  });
});
