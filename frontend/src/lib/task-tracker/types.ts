import type { UserSummary } from '$lib/chat/types';

export const TRACKER_CHANNEL_TYPE = 17 as const;

export type TrackerPriority = 'none' | 'low' | 'medium' | 'high' | 'urgent';

export interface TrackerBoard {
  custom_fields?: TrackerField[];
  channel_id: string;
  channel_domain: string;
  key_prefix: string;
  next_task_number: string;
  version: string;
  permissions: string;
  lanes: TrackerLane[];
  tasks: TrackerTask[];
}

export interface TrackerLane {
  id: string;
  origin_domain: string;
  channel_id: string;
  channel_domain: string;
  name: string;
  color: number;
  kind: string;
  completed: boolean;
  position: number;
  task_count: number;
  version: string;
}

export interface TrackerTask {
  custom_values?: TrackerValues;
  id: string;
  origin_domain: string;
  channel_id: string;
  channel_domain: string;
  lane_id: string;
  lane_domain: string;
  number: string;
  key: string;
  title: string;
  description: string | null;
  priority: TrackerPriority;
  position: number;
  due_at: string | null;
  completed_at: string | null;
  creator: UserSummary;
  assignee: UserSummary | null;
  version: string;
}

export interface CreateTrackerTaskRequest {
  custom_values?: TrackerValues;
  lane_id: string;
  title: string;
  description?: string | null;
  priority?: TrackerPriority;
  due_at?: string | null;
  assignee_id?: string | null;
  client_nonce?: string;
}

export interface UpdateTrackerTaskRequest {
  custom_values?: TrackerValues;
  title?: string;
  description?: string | null;
  priority?: TrackerPriority;
  due_at?: string | null;
  assignee_id?: string | null;
}

export interface CreateTrackerLaneRequest {
  name: string;
  color: number;
  completed?: boolean;
}

export interface UpdateTrackerLaneRequest {
  name?: string;
  color?: number;
  completed?: boolean;
}

export interface TrackerFilters {
  query: string;
  priority: TrackerPriority | 'all';
  assignee: string;
  hideCompleted: boolean;
}

export interface TrackerGatewayDispatch {
  t: string;
  d: unknown;
}

export const trackerFieldTypes = {
  text: 'Short text',
  textarea: 'Long text',
  select: 'Dropdown',
  multiselect: 'Multiple choice',
  number: 'Number',
  date: 'Date',
  checkbox: 'Checkbox',
  url: 'Link',
  users: 'People',
  channels: 'Channels',
  attachments: 'Attachments'
} as const;
export interface TrackerField {
  id: string;
  name: string;
  type: keyof typeof trackerFieldTypes;
  options: string[];
}
export interface TrackerAttachment {
  id?: string;
  name: string;
  url?: string;
  type: 'file' | 'image' | 'video';
}
export type TrackerFieldValue = string | number | boolean | string[] | TrackerAttachment[] | null;
export type TrackerValues = Record<string, TrackerFieldValue>;
