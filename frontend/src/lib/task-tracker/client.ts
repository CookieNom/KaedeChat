import { api } from '$lib/api/client';
import { entityRef, type FederatedIdentity } from '$lib/chat/refs';
import type {
  CreateTrackerLaneRequest,
  CreateTrackerTaskRequest,
  TrackerBoard,
  TrackerLane,
  TrackerTask,
  UpdateTrackerLaneRequest,
  UpdateTrackerTaskRequest
} from './types';

function trackerPath(channel: FederatedIdentity): string {
  return `/channels/${encodeURIComponent(entityRef(channel))}/tracker`;
}

function versionHeaders(version: string): HeadersInit {
  return { 'If-Match': `"${version.replaceAll('"', '')}"` };
}

export function fetchTracker(
  channel: FederatedIdentity,
  signal?: AbortSignal
): Promise<TrackerBoard> {
  return api<TrackerBoard>(trackerPath(channel), { signal });
}

export function updateTracker(
  channel: FederatedIdentity,
  version: string,
  patch: { key_prefix: string }
): Promise<TrackerBoard> {
  return api<TrackerBoard>(trackerPath(channel), {
    method: 'PATCH',
    headers: versionHeaders(version),
    body: JSON.stringify(patch)
  });
}

export function createTrackerLane(
  channel: FederatedIdentity,
  request: CreateTrackerLaneRequest
): Promise<TrackerLane> {
  return api<TrackerLane>(`${trackerPath(channel)}/lanes`, {
    method: 'POST',
    body: JSON.stringify(request)
  });
}

export function updateTrackerLane(
  channel: FederatedIdentity,
  lane: TrackerLane,
  patch: UpdateTrackerLaneRequest
): Promise<TrackerLane> {
  return api<TrackerLane>(`${trackerPath(channel)}/lanes/${encodeURIComponent(entityRef(lane))}`, {
    method: 'PATCH',
    headers: versionHeaders(lane.version),
    body: JSON.stringify(patch)
  });
}

export function deleteTrackerLane(channel: FederatedIdentity, lane: TrackerLane): Promise<void> {
  return api<void>(`${trackerPath(channel)}/lanes/${encodeURIComponent(entityRef(lane))}`, {
    method: 'DELETE',
    headers: versionHeaders(lane.version)
  });
}

export function moveTrackerLane(
  channel: FederatedIdentity,
  lane: TrackerLane,
  position: number
): Promise<TrackerLane> {
  return api<TrackerLane>(
    `${trackerPath(channel)}/lanes/${encodeURIComponent(entityRef(lane))}/move`,
    {
      method: 'POST',
      headers: versionHeaders(lane.version),
      body: JSON.stringify({ position })
    }
  );
}

export function createTrackerTask(
  channel: FederatedIdentity,
  request: CreateTrackerTaskRequest
): Promise<TrackerTask> {
  return api<TrackerTask>(`${trackerPath(channel)}/tasks`, {
    method: 'POST',
    body: JSON.stringify(request)
  });
}

export function updateTrackerTask(
  channel: FederatedIdentity,
  task: TrackerTask,
  patch: UpdateTrackerTaskRequest
): Promise<TrackerTask> {
  return api<TrackerTask>(`${trackerPath(channel)}/tasks/${encodeURIComponent(entityRef(task))}`, {
    method: 'PATCH',
    headers: versionHeaders(task.version),
    body: JSON.stringify(patch)
  });
}

export function deleteTrackerTask(channel: FederatedIdentity, task: TrackerTask): Promise<void> {
  return api<void>(`${trackerPath(channel)}/tasks/${encodeURIComponent(entityRef(task))}`, {
    method: 'DELETE',
    headers: versionHeaders(task.version)
  });
}

export function moveTrackerTask(
  channel: FederatedIdentity,
  task: TrackerTask,
  lane: TrackerLane,
  position: number
): Promise<TrackerTask> {
  return api<TrackerTask>(
    `${trackerPath(channel)}/tasks/${encodeURIComponent(entityRef(task))}/move`,
    {
      method: 'POST',
      headers: versionHeaders(task.version),
      body: JSON.stringify({ lane_id: entityRef(lane), position })
    }
  );
}
