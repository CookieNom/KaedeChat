export type PresencePreference = 'online' | 'idle' | 'dnd' | 'invisible';
export type PresenceSource = 'manual' | 'timed' | 'schedule' | 'override';

export interface PresenceScheduleRule {
  id: string;
  name: string;
  status: PresencePreference;
  days: number[]; // 0=Sun .. 6=Sat
  start_minute: number; // 0..1439
  end_minute: number; // 0..1439
  enabled: boolean;
}

export interface PresenceStatePayload {
  effective_status: PresencePreference;
  suppress_notifications: boolean;
  source: PresenceSource;
  base_status: PresencePreference;
  timed_status: PresencePreference | null;
  timed_expires_at: string | null;
  active_schedule_id: string | null;
  active_schedule_name: string | null;
  active_schedule_status: PresencePreference | null;
  active_schedule_ends_at: string | null;
  skipped_schedule_name: string | null;
  skipped_schedule_until: string | null;
  timezone: string;
  schedules: PresenceScheduleRule[];
}

export interface PresenceDurationOption {
  minutes: number | null;
  label: string;
}

export const PRESENCE_DURATION_OPTIONS: readonly PresenceDurationOption[] = [
  { minutes: 15, label: 'For 15 Minutes' },
  { minutes: 60, label: 'For 1 Hour' },
  { minutes: 480, label: 'For 8 Hours' },
  { minutes: 1440, label: 'For 24 Hours' },
  { minutes: 4320, label: 'For 3 Days' },
  { minutes: null, label: 'Forever' }
];

export const MAX_PRESENCE_SCHEDULES = 10;

export const DAY_SHORT_LABELS = ['S', 'M', 'T', 'W', 'T', 'F', 'S'] as const;
export const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const;

export const COMMON_TIMEZONES: readonly string[] = [
  'UTC',
  'Pacific/Honolulu',
  'America/Anchorage',
  'America/Los_Angeles',
  'America/Denver',
  'America/Chicago',
  'America/New_York',
  'America/Sao_Paulo',
  'Europe/London',
  'Europe/Paris',
  'Europe/Berlin',
  'Europe/Helsinki',
  'Asia/Dubai',
  'Asia/Kolkata',
  'Asia/Bangkok',
  'Asia/Singapore',
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Asia/Seoul',
  'Australia/Sydney',
  'Pacific/Auckland'
];

export function detectDeviceTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

export function formatMinuteOfDay(minute: number): string {
  const normalized = ((minute % 1440) + 1440) % 1440;
  const h24 = Math.floor(normalized / 60);
  const mins = normalized % 60;
  const suffix = h24 >= 12 ? 'PM' : 'AM';
  const h12 = h24 % 12 || 12;
  return `${h12}:${String(mins).padStart(2, '0')} ${suffix}`;
}

export function minuteToTimeInput(minute: number): string {
  const normalized = ((minute % 1440) + 1440) % 1440;
  const h = Math.floor(normalized / 60);
  const m = normalized % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

export function timeInputToMinute(value: string): number {
  const [hRaw, mRaw] = value.split(':');
  const h = Number(hRaw);
  const m = Number(mRaw);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return 0;
  return Math.max(0, Math.min(23, Math.trunc(h))) * 60 + Math.max(0, Math.min(59, Math.trunc(m)));
}

export function formatDaysSummary(days: number[]): string {
  const unique = days
    .filter((d, index) => d >= 0 && d <= 6 && days.indexOf(d) === index)
    .sort((a, b) => a - b);
  if (unique.length === 7) return 'Every day';
  if (unique.length === 5 && [1, 2, 3, 4, 5].every((d) => unique.includes(d))) return 'Weekdays';
  if (unique.length === 2 && unique.includes(0) && unique.includes(6)) return 'Weekends';
  return unique.map((d) => DAY_NAMES[d]).join(', ') || 'No days';
}

export function formatTimestampHint(isoUtc: string | null, nowMs = Date.now()): string {
  if (!isoUtc) return '';
  const targetMs = Date.parse(isoUtc);
  if (!Number.isFinite(targetMs)) return '';
  const diffMinutes = Math.max(1, Math.round((targetMs - nowMs) / 60_000));
  if (diffMinutes < 60) return `${diffMinutes}m left`;
  if (diffMinutes < 20 * 60) {
    try {
      const formatted = new Intl.DateTimeFormat(undefined, {
        hour: 'numeric',
        minute: '2-digit'
      }).format(targetMs);
      return `Until ${formatted}`;
    } catch {
      return `${Math.round(diffMinutes / 60)}h left`;
    }
  }
  const days = Math.floor(diffMinutes / 1440);
  const hours = Math.round((diffMinutes % 1440) / 60);
  if (days >= 1) return hours > 0 ? `${days}d ${hours}h left` : `${days}d left`;
  return `${Math.round(diffMinutes / 60)}h left`;
}

export function scheduleMatchesAt(
  rule: PresenceScheduleRule,
  day: number,
  minute: number
): boolean {
  if (!rule.enabled || rule.days.length === 0 || rule.start_minute === rule.end_minute) {
    return false;
  }
  if (rule.start_minute < rule.end_minute) {
    return rule.days.includes(day) && minute >= rule.start_minute && minute < rule.end_minute;
  }
  const yesterday = (day + 6) % 7;
  return (
    (rule.days.includes(day) && minute >= rule.start_minute) ||
    (rule.days.includes(yesterday) && minute < rule.end_minute)
  );
}

export function presenceStatusLabel(status: PresencePreference): string {
  switch (status) {
    case 'online':
      return 'Online';
    case 'idle':
      return 'Idle';
    case 'dnd':
      return 'Do Not Disturb';
    case 'invisible':
      return 'Invisible';
  }
}

export function summarizePresenceSubtext(
  state: PresenceStatePayload | null,
  nowMs = Date.now()
): string {
  if (!state) return '';
  if (state.source === 'timed' && state.timed_expires_at) {
    return formatTimestampHint(state.timed_expires_at, nowMs);
  }
  if (state.source === 'schedule' && state.active_schedule_name) {
    const until = formatTimestampHint(state.active_schedule_ends_at, nowMs);
    return until ? `${state.active_schedule_name} · ${until}` : state.active_schedule_name;
  }
  if (state.source === 'override' && state.skipped_schedule_name) {
    const until = formatTimestampHint(state.skipped_schedule_until, nowMs);
    return until
      ? `Skipping ${state.skipped_schedule_name} · ${until}`
      : `Skipping ${state.skipped_schedule_name}`;
  }
  if (
    state.effective_status === 'invisible' &&
    state.suppress_notifications &&
    state.active_schedule_name
  ) {
    return `${state.active_schedule_name} · Alerts muted`;
  }
  return '';
}

export function summarizeSchedulesHint(state: PresenceStatePayload | null): string {
  const enabled = (state?.schedules ?? []).filter((rule) => rule.enabled);
  if (enabled.length === 0) return 'Off · Set quiet hours';
  if (enabled.length === 1) {
    const only = enabled[0];
    return `${only.name} · ${formatMinuteOfDay(only.start_minute)}–${formatMinuteOfDay(only.end_minute)}`;
  }
  return `${enabled.length} active schedules`;
}

export function isPresenceStatePayload(value: unknown): value is PresenceStatePayload {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.effective_status === 'string' &&
    typeof candidate.suppress_notifications === 'boolean' &&
    Array.isArray(candidate.schedules)
  );
}

class PresenceControlStore {
  state = $state<PresenceStatePayload | null>(null);

  apply(payload: PresenceStatePayload | null | undefined): void {
    if (!payload) return;
    this.state = payload;
  }

  applyFallbackPreference(preference: PresencePreference): void {
    if (this.state && this.state.effective_status === preference) return;
    this.state = this.state
      ? {
          ...this.state,
          effective_status: preference,
          suppress_notifications: preference === 'dnd'
        }
      : {
          effective_status: preference,
          suppress_notifications: preference === 'dnd',
          source: 'manual',
          base_status: preference,
          timed_status: null,
          timed_expires_at: null,
          active_schedule_id: null,
          active_schedule_name: null,
          active_schedule_status: null,
          active_schedule_ends_at: null,
          skipped_schedule_name: null,
          skipped_schedule_until: null,
          timezone: detectDeviceTimezone(),
          schedules: []
        };
  }

  reset(): void {
    this.state = null;
  }
}

export const presenceControl = new PresenceControlStore();
