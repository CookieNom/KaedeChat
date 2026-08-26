import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: apiMock }));

import {
  createTrackerTask,
  deleteTrackerLane,
  fetchTracker,
  moveTrackerTask,
  updateTracker
} from './client';
import type { TrackerLane, TrackerTask } from './types';

const channel = { id: '10', origin_domain: 'chat.example' };
const lane = {
  id: '20',
  origin_domain: 'chat.example',
  channel_id: '10',
  channel_domain: 'chat.example',
  name: 'Planned',
  color: 0xffaa00,
  kind: 'planned',
  completed: false,
  position: 0,
  task_count: 1,
  version: '2026-08-26T12:00:00+00:00'
} satisfies TrackerLane;
const task = {
  id: '30',
  origin_domain: 'chat.example',
  channel_id: '10',
  channel_domain: 'chat.example',
  lane_id: '20',
  lane_domain: 'chat.example',
  number: '1',
  key: 'PLAN-1',
  title: 'Ship it',
  description: null,
  priority: 'high',
  position: 0,
  due_at: null,
  completed_at: null,
  creator: {
    id: '40',
    origin_domain: 'chat.example',
    username: 'owner',
    display_name: 'Owner',
    avatar_hash: null,
    handle: 'owner@chat.example'
  },
  assignee: null,
  version: '2026-08-26T12:01:00+00:00'
} satisfies TrackerTask;

describe('task tracker API client', () => {
  beforeEach(() => apiMock.mockReset());

  it('uses canonical composite channel refs for board reads', async () => {
    await fetchTracker(channel);
    expect(apiMock).toHaveBeenCalledWith('/channels/10%40chat.example/tracker', {
      signal: undefined
    });
  });

  it('sends quoted resource versions for optimistic concurrency', async () => {
    await updateTracker(channel, '2026-08-26T12:00:00+00:00', { key_prefix: 'RAID' });
    expect(apiMock).toHaveBeenCalledWith('/channels/10%40chat.example/tracker', {
      method: 'PATCH',
      headers: { 'If-Match': '"2026-08-26T12:00:00+00:00"' },
      body: JSON.stringify({ key_prefix: 'RAID' })
    });

    await deleteTrackerLane(channel, lane);
    expect(apiMock).toHaveBeenLastCalledWith(
      '/channels/10%40chat.example/tracker/lanes/20%40chat.example',
      {
        method: 'DELETE',
        headers: { 'If-Match': '"2026-08-26T12:00:00+00:00"' }
      }
    );
  });

  it('preserves federated lane and assignee refs on task writes', async () => {
    await createTrackerTask(channel, {
      lane_id: '20@chat.example',
      title: 'Ship it',
      assignee_id: '40@users.example',
      client_nonce: 'create-1'
    });
    expect(apiMock).toHaveBeenCalledWith('/channels/10%40chat.example/tracker/tasks', {
      method: 'POST',
      body: JSON.stringify({
        lane_id: '20@chat.example',
        title: 'Ship it',
        assignee_id: '40@users.example',
        client_nonce: 'create-1'
      })
    });

    await moveTrackerTask(channel, task, lane, 2);
    expect(apiMock).toHaveBeenLastCalledWith(
      '/channels/10%40chat.example/tracker/tasks/30%40chat.example/move',
      {
        method: 'POST',
        headers: { 'If-Match': '"2026-08-26T12:01:00+00:00"' },
        body: JSON.stringify({ lane_id: '20@chat.example', position: 2 })
      }
    );
  });
});
