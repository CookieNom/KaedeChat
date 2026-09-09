import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: apiMock }));

import {
  ScheduledEventEntityType,
  ScheduledEventRecurrencePreset,
  ScheduledEventStatus,
  createScheduledEvent,
  listScheduledEventUsers,
  scheduledEventPatch,
  scheduledEventPayload,
  scheduledEventRecurrencePreset,
  scheduledEventSubscriptionState,
  setScheduledEventSubscription,
  transitionScheduledEvent,
  type ScheduledEvent,
  type ScheduledEventDraft,
  type ScheduledEventRecurrenceRule
} from './scheduled-events';

const voiceDraft: ScheduledEventDraft = {
  name: 'Town hall',
  description: 'Quarterly community questions',
  entityType: ScheduledEventEntityType.voice,
  channelRef: '9@guild.example',
  location: '',
  startTime: '2027-01-03T18:00:00.000Z',
  endTime: ''
};

const event: ScheduledEvent = {
  id: '50',
  origin_domain: 'guild.example',
  guild_id: '1',
  guild_domain: 'guild.example',
  channel_id: '9',
  channel_domain: 'guild.example',
  creator_id: '4',
  creator_domain: 'guild.example',
  name: 'Town hall',
  description: 'Quarterly community questions',
  scheduled_start_time: '2027-01-03T18:00:00.000Z',
  scheduled_end_time: null,
  privacy_level: 2,
  status: ScheduledEventStatus.scheduled,
  entity_type: ScheduledEventEntityType.voice,
  entity_id: null,
  entity_domain: null,
  entity_metadata: null,
  recurrence_rule: null,
  image: null,
  created_at: '2027-01-01T00:00:00.000Z',
  updated_at: '2027-01-01T00:00:00.000Z',
  version: '1',
  user_count: 3,
  me_subscribed: true
};

describe('scheduled event client', () => {
  beforeEach(() => apiMock.mockReset());

  it('initializes follow state from the viewer projection while honoring local changes', () => {
    expect(scheduledEventSubscriptionState(event)).toBe(true);
    expect(scheduledEventSubscriptionState(event, false)).toBe(false);
    expect(scheduledEventSubscriptionState({})).toBe(false);
  });

  it('serializes stage, voice, and external fields with Discord-compatible entity values', () => {
    expect(scheduledEventPayload(voiceDraft)).toEqual({
      channel_id: '9@guild.example',
      entity_metadata: null,
      name: 'Town hall',
      privacy_level: 2,
      scheduled_start_time: '2027-01-03T18:00:00.000Z',
      scheduled_end_time: null,
      description: 'Quarterly community questions',
      entity_type: 2,
      recurrence_rule: null
    });
    expect(
      scheduledEventPayload({
        ...voiceDraft,
        entityType: ScheduledEventEntityType.stage,
        channelRef: '10@guild.example'
      })
    ).toMatchObject({
      channel_id: '10@guild.example',
      entity_metadata: null,
      entity_type: 1
    });
    expect(
      scheduledEventPayload({
        ...voiceDraft,
        entityType: ScheduledEventEntityType.external,
        channelRef: '',
        location: 'https://conference.example/keynote',
        endTime: '2027-01-03T20:00:00.000Z'
      })
    ).toMatchObject({
      channel_id: null,
      entity_metadata: { location: 'https://conference.example/keynote' },
      entity_type: 3
    });
  });

  it('serializes Discord recurrence presets from the event start in UTC', () => {
    expect(
      scheduledEventPayload({ ...voiceDraft, recurrence: ScheduledEventRecurrencePreset.weekly })
        .recurrence_rule
    ).toEqual({
      start: '2027-01-03T18:00:00.000Z',
      end: null,
      frequency: 2,
      interval: 1,
      by_weekday: [6]
    });
    expect(
      scheduledEventPayload({ ...voiceDraft, recurrence: ScheduledEventRecurrencePreset.yearly })
        .recurrence_rule
    ).toMatchObject({ frequency: 0, by_month: [1], by_month_day: [3] });
    expect(
      scheduledEventRecurrencePreset({
        start: '2027-01-03T18:00:00.000Z',
        end: null,
        frequency: 99,
        interval: 1
      } as unknown as ScheduledEventRecurrenceRule)
    ).toBe(ScheduledEventRecurrencePreset.none);
  });

  it('gives clear validation errors for invalid external timing', () => {
    expect(() =>
      scheduledEventPayload({
        ...voiceDraft,
        entityType: ScheduledEventEntityType.external,
        channelRef: '',
        location: 'Convention center',
        endTime: ''
      })
    ).toThrow(/end time/i);
    expect(() =>
      scheduledEventPayload({
        ...voiceDraft,
        entityType: ScheduledEventEntityType.external,
        channelRef: '',
        location: 'Convention center',
        endTime: '2027-01-03T17:59:00.000Z'
      })
    ).toThrow(/end time.*start time/i);
  });

  it('does not resend unchanged past-sensitive fields while editing', () => {
    expect(
      scheduledEventPatch(event, {
        ...voiceDraft,
        name: 'Community town hall'
      })
    ).toEqual({ name: 'Community town hall' });
  });

  it('uses canonical event routes for create, transition, subscribers, and self subscription', async () => {
    const active = { ...event, status: ScheduledEventStatus.active };
    const subscribers = [
      {
        guild_scheduled_event_id: '50',
        guild_scheduled_event_domain: 'guild.example',
        user: {
          id: '8',
          origin_domain: 'remote.example',
          username: 'guest',
          display_name: null,
          avatar_hash: null,
          handle: 'guest@remote.example'
        },
        member: null,
        subscribed_at: '2027-01-02T00:00:00Z'
      }
    ];
    apiMock
      .mockResolvedValueOnce(event)
      .mockResolvedValueOnce(active)
      .mockResolvedValueOnce(subscribers)
      .mockResolvedValueOnce(undefined)
      .mockResolvedValueOnce(undefined);
    expect(await createScheduledEvent('1@guild.example', voiceDraft)).toEqual(event);
    expect(
      await transitionScheduledEvent('1@guild.example', event, ScheduledEventStatus.active)
    ).toEqual(active);
    expect(
      await listScheduledEventUsers('1@guild.example', event, {
        after: '7@remote.example',
        limit: 25
      })
    ).toEqual(subscribers);
    expect(await setScheduledEventSubscription('1@guild.example', event, true)).toBeUndefined();
    expect(await setScheduledEventSubscription('1@guild.example', event, false)).toBeUndefined();

    expect(apiMock.mock.calls.map(([path]) => path)).toEqual([
      '/guilds/1%40guild.example/scheduled-events',
      '/guilds/1%40guild.example/scheduled-events/50%40guild.example',
      '/guilds/1%40guild.example/scheduled-events/50%40guild.example/users?limit=25&with_member=true&after=7%40remote.example',
      '/guilds/1%40guild.example/scheduled-events/50%40guild.example/users/@me',
      '/guilds/1%40guild.example/scheduled-events/50%40guild.example/users/@me'
    ]);
    expect(apiMock.mock.calls[0][1].method).toBe('POST');
    expect(JSON.parse(apiMock.mock.calls[0][1].body)).toMatchObject({
      name: 'Town hall',
      description: 'Quarterly community questions',
      channel_id: '9@guild.example',
      entity_type: 2,
      privacy_level: 2,
      scheduled_start_time: '2027-01-03T18:00:00.000Z'
    });
    expect(apiMock.mock.calls[1][1]).toMatchObject({
      method: 'PATCH',
      body: JSON.stringify({ status: 2 })
    });
    expect(apiMock.mock.calls[3][1]).toMatchObject({ method: 'PUT' });
    expect(apiMock.mock.calls[4][1]).toMatchObject({ method: 'DELETE' });
  });
});
