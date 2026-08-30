import { api } from '$lib/api/client';
import { completeScannedMediaResource } from '$lib/media/scanned';
import { uploadObject, type UploadTicket } from '$lib/media/uploads';
import type { GuildMemberSummary, UserSummary } from './types';

export const ScheduledEventStatus = {
  scheduled: 1,
  active: 2,
  completed: 3,
  canceled: 4
} as const;

export const ScheduledEventEntityType = {
  stage: 1,
  voice: 2,
  external: 3
} as const;

export type ScheduledEventStatusValue =
  (typeof ScheduledEventStatus)[keyof typeof ScheduledEventStatus];
export type ScheduledEventEntityTypeValue =
  (typeof ScheduledEventEntityType)[keyof typeof ScheduledEventEntityType];

export const ScheduledEventRecurrencePreset = {
  none: 'none',
  daily: 'daily',
  weekly: 'weekly',
  biweekly: 'biweekly',
  monthly: 'monthly',
  yearly: 'yearly'
} as const;

export const ScheduledEventRecurrenceFrequency = {
  yearly: 0,
  monthly: 1,
  weekly: 2,
  daily: 3
} as const;

export type ScheduledEventRecurrencePresetValue =
  (typeof ScheduledEventRecurrencePreset)[keyof typeof ScheduledEventRecurrencePreset];

export interface ScheduledEventRecurrenceRule {
  start: string;
  end: null;
  frequency: 0 | 1 | 2 | 3;
  interval: 1 | 2;
  by_weekday?: number[] | null;
  by_n_weekday?: { n: number; day: number }[] | null;
  by_month?: number[] | null;
  by_month_day?: number[] | null;
  by_year_day?: null;
  count?: null;
}

export interface ScheduledEvent {
  id: string;
  origin_domain: string;
  guild_id: string;
  guild_domain: string;
  channel_id: string | null;
  channel_domain: string | null;
  creator_id: string;
  creator_domain: string;
  creator?: UserSummary;
  name: string;
  description: string | null;
  scheduled_start_time: string;
  scheduled_end_time: string | null;
  privacy_level: 2;
  status: ScheduledEventStatusValue;
  entity_type: ScheduledEventEntityTypeValue;
  entity_id: string | null;
  entity_domain: string | null;
  entity_metadata: { location: string } | null;
  recurrence_rule: ScheduledEventRecurrenceRule | null;
  image: string | null;
  created_at: string;
  updated_at: string;
  version: string;
  user_count?: number;
  me_subscribed?: boolean;
}

export interface ScheduledEventUser {
  guild_scheduled_event_id: string;
  guild_scheduled_event_domain: string;
  user: UserSummary;
  member: GuildMemberSummary | null;
  subscribed_at: string;
}

export interface ScheduledEventDraft {
  name: string;
  description: string;
  entityType: ScheduledEventEntityTypeValue;
  channelRef: string;
  location: string;
  startTime: string;
  endTime: string;
  recurrence?: ScheduledEventRecurrencePresetValue;
}

export function scheduledEventRef(event: Pick<ScheduledEvent, 'id' | 'origin_domain'>): string {
  return `${event.id}@${event.origin_domain}`;
}

export function scheduledEventSubscriptionState(
  event: Pick<ScheduledEvent, 'me_subscribed'>,
  localOverride?: boolean
): boolean {
  return localOverride ?? event.me_subscribed ?? false;
}

export function eventChannelRef(
  event: Pick<ScheduledEvent, 'channel_id' | 'channel_domain'>
): string | null {
  return event.channel_id && event.channel_domain
    ? `${event.channel_id}@${event.channel_domain}`
    : null;
}

export function scheduledEventStatusLabel(status: ScheduledEventStatusValue): string {
  if (status === ScheduledEventStatus.active) return 'Live';
  if (status === ScheduledEventStatus.completed) return 'Completed';
  if (status === ScheduledEventStatus.canceled) return 'Canceled';
  return 'Scheduled';
}

export function scheduledEventRecurrencePreset(
  rule: ScheduledEventRecurrenceRule | null | undefined
): ScheduledEventRecurrencePresetValue {
  if (!rule) return ScheduledEventRecurrencePreset.none;
  if (rule.frequency === ScheduledEventRecurrenceFrequency.daily)
    return ScheduledEventRecurrencePreset.daily;
  if (rule.frequency === ScheduledEventRecurrenceFrequency.weekly)
    return rule.interval === 2
      ? ScheduledEventRecurrencePreset.biweekly
      : ScheduledEventRecurrencePreset.weekly;
  if (rule.frequency === ScheduledEventRecurrenceFrequency.monthly)
    return ScheduledEventRecurrencePreset.monthly;
  if (rule.frequency === ScheduledEventRecurrenceFrequency.yearly)
    return ScheduledEventRecurrencePreset.yearly;
  return ScheduledEventRecurrencePreset.none;
}

export function scheduledEventRecurrenceLabel(
  rule: ScheduledEventRecurrenceRule | null | undefined
): string | null {
  const preset = scheduledEventRecurrencePreset(rule);
  if (preset === ScheduledEventRecurrencePreset.none) return null;
  if (preset === ScheduledEventRecurrencePreset.daily) return 'Repeats daily';
  if (preset === ScheduledEventRecurrencePreset.weekly) return 'Repeats weekly';
  if (preset === ScheduledEventRecurrencePreset.biweekly) return 'Repeats every 2 weeks';
  if (preset === ScheduledEventRecurrencePreset.monthly) return 'Repeats monthly';
  return 'Repeats yearly';
}

function isoTimestamp(value: string, field: string): string {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.valueOf())) throw new Error(`Choose a valid ${field}.`);
  return date.toISOString();
}

function recurrenceRule(
  preset: ScheduledEventRecurrencePresetValue | undefined,
  scheduledStartTime: string
): ScheduledEventRecurrenceRule | null {
  if (!preset || preset === ScheduledEventRecurrencePreset.none) return null;
  const start = new Date(scheduledStartTime);
  const common = { start: start.toISOString(), end: null, interval: 1 as const };
  if (preset === ScheduledEventRecurrencePreset.daily)
    return { ...common, frequency: ScheduledEventRecurrenceFrequency.daily };
  if (preset === ScheduledEventRecurrencePreset.weekly) {
    return {
      ...common,
      frequency: ScheduledEventRecurrenceFrequency.weekly,
      by_weekday: [(start.getUTCDay() + 6) % 7]
    };
  }
  if (preset === ScheduledEventRecurrencePreset.biweekly) {
    return {
      ...common,
      frequency: ScheduledEventRecurrenceFrequency.weekly,
      interval: 2,
      by_weekday: [(start.getUTCDay() + 6) % 7]
    };
  }
  if (preset === ScheduledEventRecurrencePreset.monthly)
    return { ...common, frequency: ScheduledEventRecurrenceFrequency.monthly };
  return {
    ...common,
    frequency: ScheduledEventRecurrenceFrequency.yearly,
    by_month: [start.getUTCMonth() + 1],
    by_month_day: [start.getUTCDate()]
  };
}

export function scheduledEventPayload(draft: ScheduledEventDraft): Record<string, unknown> {
  const name = draft.name.trim();
  const description = draft.description.trim();
  if (!name) throw new Error('Give the scheduled event a name.');
  const scheduledStartTime = isoTimestamp(draft.startTime, 'start date and time');
  const scheduledEndTime = draft.endTime ? isoTimestamp(draft.endTime, 'end date and time') : null;
  if (scheduledEndTime && Date.parse(scheduledEndTime) <= Date.parse(scheduledStartTime)) {
    throw new Error('The end time must be later than the start time.');
  }

  if (draft.entityType !== ScheduledEventEntityType.external) {
    if (!draft.channelRef) throw new Error('Choose the channel where this event will happen.');
    return {
      channel_id: draft.channelRef,
      entity_metadata: null,
      name,
      privacy_level: 2,
      scheduled_start_time: scheduledStartTime,
      scheduled_end_time: scheduledEndTime,
      description: description || null,
      entity_type: draft.entityType,
      recurrence_rule: recurrenceRule(draft.recurrence, scheduledStartTime)
    };
  }

  const location = draft.location.trim();
  if (!location) throw new Error('Add the location or link for this external event.');
  if (!scheduledEndTime) throw new Error('Choose an end time for this external event.');
  return {
    channel_id: null,
    entity_metadata: { location },
    name,
    privacy_level: 2,
    scheduled_start_time: scheduledStartTime,
    scheduled_end_time: scheduledEndTime,
    description: description || null,
    entity_type: ScheduledEventEntityType.external,
    recurrence_rule: recurrenceRule(draft.recurrence, scheduledStartTime)
  };
}

/** Build a PATCH that never resends an unchanged, already-past start time. */
export function scheduledEventPatch(
  event: ScheduledEvent,
  draft: ScheduledEventDraft
): Record<string, unknown> {
  const next = scheduledEventPayload(draft);
  const previous: Record<string, unknown> = {
    channel_id: eventChannelRef(event),
    entity_metadata: event.entity_metadata,
    name: event.name,
    privacy_level: event.privacy_level,
    scheduled_start_time: new Date(event.scheduled_start_time).toISOString(),
    scheduled_end_time: event.scheduled_end_time
      ? new Date(event.scheduled_end_time).toISOString()
      : null,
    description: event.description,
    entity_type: event.entity_type,
    recurrence_rule: recurrenceRule(
      scheduledEventRecurrencePreset(event.recurrence_rule),
      event.scheduled_start_time
    )
  };
  return Object.fromEntries(
    Object.entries(next).filter(
      ([key, value]) => JSON.stringify(value) !== JSON.stringify(previous[key])
    )
  );
}

function collectionPath(guildRef: string): string {
  return `/guilds/${encodeURIComponent(guildRef)}/scheduled-events`;
}

function eventPath(guildRef: string, eventRef: string): string {
  return `${collectionPath(guildRef)}/${encodeURIComponent(eventRef)}`;
}

function eventImagePath(guildRef: string, event: ScheduledEvent): string {
  return `${eventPath(guildRef, scheduledEventRef(event))}/image`;
}

export async function uploadScheduledEventImage(
  guildRef: string,
  event: ScheduledEvent,
  file: File,
  onProgress: (progress: number) => void = () => undefined
): Promise<ScheduledEvent> {
  if (!file.type.toLowerCase().startsWith('image/')) throw new Error('Choose an image file.');
  if (file.size < 1 || file.size > 10 * 1024 * 1024) {
    throw new Error('Scheduled event cover images must be between 1 byte and 10 MiB.');
  }
  const path = eventImagePath(guildRef, event);
  const ticket = await api<UploadTicket>(`${path}/tickets`, {
    method: 'POST',
    body: JSON.stringify({
      filename: file.name || 'event-cover',
      content_type: file.type,
      size: file.size
    })
  });
  await uploadObject(ticket, file, onProgress);
  return completeScannedMediaResource(
    () =>
      api<ScheduledEvent | { status: string; attachment: { scan_status: string } }>(path, {
        method: 'PUT',
        body: JSON.stringify({ attachment_id: ticket.id })
      }),
    (value): value is ScheduledEvent => 'guild_id' in value && 'image' in value,
    {
      maxAttempts: 46,
      rejectedMessage: 'The event cover did not pass media processing. Choose another image.',
      timeoutMessage: 'The event cover is still processing. Try again shortly.'
    }
  );
}

export function deleteScheduledEventImage(
  guildRef: string,
  event: ScheduledEvent
): Promise<ScheduledEvent> {
  return api<ScheduledEvent>(eventImagePath(guildRef, event), { method: 'DELETE' });
}

export function listScheduledEvents(
  guildRef: string,
  signal?: AbortSignal
): Promise<ScheduledEvent[]> {
  return api<ScheduledEvent[]>(`${collectionPath(guildRef)}?with_user_count=true`, { signal });
}

export function createScheduledEvent(
  guildRef: string,
  draft: ScheduledEventDraft
): Promise<ScheduledEvent> {
  return api<ScheduledEvent>(collectionPath(guildRef), {
    method: 'POST',
    body: JSON.stringify(scheduledEventPayload(draft))
  });
}

export function editScheduledEvent(
  guildRef: string,
  event: ScheduledEvent,
  draft: ScheduledEventDraft
): Promise<ScheduledEvent> {
  const patch = scheduledEventPatch(event, draft);
  if (!Object.keys(patch).length) return Promise.resolve(event);
  return api<ScheduledEvent>(eventPath(guildRef, scheduledEventRef(event)), {
    method: 'PATCH',
    body: JSON.stringify(patch)
  });
}

export function transitionScheduledEvent(
  guildRef: string,
  event: ScheduledEvent,
  status: 2 | 3 | 4
): Promise<ScheduledEvent> {
  return api<ScheduledEvent>(eventPath(guildRef, scheduledEventRef(event)), {
    method: 'PATCH',
    body: JSON.stringify({ status })
  });
}

export function deleteScheduledEvent(guildRef: string, event: ScheduledEvent): Promise<void> {
  return api<void>(eventPath(guildRef, scheduledEventRef(event)), { method: 'DELETE' });
}

export function listScheduledEventUsers(
  guildRef: string,
  event: ScheduledEvent,
  options: { after?: string; limit?: number; signal?: AbortSignal } = {}
): Promise<ScheduledEventUser[]> {
  const query = new URLSearchParams({
    limit: String(options.limit ?? 100),
    with_member: 'true'
  });
  if (options.after) query.set('after', options.after);
  return api<ScheduledEventUser[]>(
    `${eventPath(guildRef, scheduledEventRef(event))}/users?${query.toString()}`,
    { signal: options.signal }
  );
}

export function setScheduledEventSubscription(
  guildRef: string,
  event: ScheduledEvent,
  subscribed: boolean
): Promise<void> {
  return api<void>(`${eventPath(guildRef, scheduledEventRef(event))}/users/@me`, {
    method: subscribed ? 'PUT' : 'DELETE'
  });
}
