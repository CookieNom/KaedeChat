import { describe, expect, it } from 'vitest';
import {
  AUDIT_ACTION_OPTIONS,
  auditActionFilterValue,
  auditActionLabel,
  auditChangeDescription,
  auditFieldLabel,
  auditLogQueryString,
  auditRelativeTime,
  auditSummary,
  auditTargetName,
  canonicalAuditActorRef,
  parseAuditActionFilter,
  type AuditLogEntry
} from './audit';
import type { Guild } from './types';

const entry = {
  id: '9',
  guild_id: '1',
  guild_domain: 'chat.example',
  actor_id: '2',
  actor_domain: 'chat.example',
  action_type: 25,
  target_type: 'instance',
  target_ref: { domain: 'blocked.example' },
  reason: null,
  changes: [],
  created_at: '2026-08-26T00:00:00Z'
} satisfies AuditLogEntry;

describe('guild audit helpers', () => {
  it('disambiguates action codes by target type', () => {
    expect(auditActionLabel(entry)).toBe('Instance banned');
    expect(auditActionLabel({ ...entry, target_type: 'member' })).toBe('Member roles updated');
  });

  it('renders a readable actor/action/target summary', () => {
    expect(auditSummary('Mika', entry, 'blocked.example')).toBe('Mika banned blocked.example');
  });

  it('labels Discord-compatible scheduled-event audit actions', () => {
    const scheduledEvent = {
      ...entry,
      action_type: 100,
      target_type: 'scheduled_event'
    };
    expect(auditActionLabel(scheduledEvent)).toBe('Scheduled event created');
    expect(auditSummary('Mika', { ...scheduledEvent, action_type: 101 }, 'a scheduled event')).toBe(
      'Mika updated a scheduled event'
    );
  });

  it('covers the extended Discord-style moderation and resource actions', () => {
    expect(auditActionLabel({ action_type: 21, target_type: 'guild' })).toBe('Members pruned');
    expect(auditActionLabel({ action_type: 90, target_type: 'sticker' })).toBe('Sticker created');
    expect(auditActionLabel({ action_type: 110, target_type: 'thread' })).toBe('Thread created');
    expect(auditActionLabel({ action_type: 130, target_type: 'soundboard_sound' })).toBe(
      'Soundboard sound created'
    );
    expect(auditActionLabel({ action_type: 140, target_type: 'auto_mod_rule' })).toBe(
      'AutoMod rule created'
    );
    expect(auditActionLabel({ action_type: 192, target_type: 'voice_channel' })).toBe(
      'Voice channel status set'
    );
  });

  it('keeps every action/target filter combination unique', () => {
    const keys = AUDIT_ACTION_OPTIONS.map(auditActionFilterValue);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('builds server-side actor, action, target, and pagination filters', () => {
    const selected = parseAuditActionFilter('140|auto_mod_rule');
    expect(selected).toEqual({ action_type: 140, target_type: 'auto_mod_rule' });

    const query = new URLSearchParams(
      auditLogQueryString({
        limit: 100,
        before: '999',
        userId: '2@chat.example',
        actionType: selected!.action_type,
        targetType: selected!.target_type
      })
    );
    expect(Object.fromEntries(query)).toEqual({
      limit: '100',
      before: '999',
      user_id: '2@chat.example',
      action_type: '140',
      target_type: 'auto_mod_rule'
    });
  });

  it('accepts canonical departed-actor references without accepting search text', () => {
    expect(canonicalAuditActorRef(' 7@REMOTE.Example ')).toBe('7@remote.example');
    expect(canonicalAuditActorRef('0@chat.example')).toBe('0@chat.example');
    expect(canonicalAuditActorRef('9223372036854775807@chat.example')).toBe(
      '9223372036854775807@chat.example'
    );
    expect(canonicalAuditActorRef('9223372036854775808@chat.example')).toBeNull();
    expect(canonicalAuditActorRef('Kaede')).toBeNull();
    expect(canonicalAuditActorRef('7@localhost')).toBeNull();
    expect(canonicalAuditActorRef('07@chat.example')).toBeNull();
  });

  it('rejects malformed action filter values', () => {
    expect(parseAuditActionFilter('')).toBeNull();
    expect(parseAuditActionFilter('thread')).toBeNull();
    expect(parseAuditActionFilter('-1|thread')).toBeNull();
  });

  it('renders named threads and requested resource targets clearly', () => {
    const guild = {
      id: '1',
      origin_domain: 'chat.example',
      name: 'Garden',
      description: null,
      icon_hash: null,
      owner_id: '2',
      permission_generation: '1',
      unavailable: false,
      channels: [],
      roles: [],
      emojis: [],
      stickers: []
    } satisfies Guild;
    expect(
      auditTargetName(
        { ...entry, action_type: 110, target_type: 'thread', target_ref: { name: 'roadmap' } },
        guild,
        []
      )
    ).toBe('thread #roadmap');
    expect(
      auditTargetName(
        {
          ...entry,
          action_type: 130,
          target_type: 'soundboard_sound',
          target_ref: { name: 'Air horn' }
        },
        guild,
        []
      )
    ).toBe('sound Air horn');
    expect(
      auditTargetName(
        { ...entry, action_type: 21, target_type: 'guild', target_ref: null },
        guild,
        []
      )
    ).toBe('inactive members');
  });

  it('formats before/after and collection changes', () => {
    expect(auditChangeDescription({ key: 'name', old_value: 'old', new_value: 'new' })).toBe(
      'old → new'
    );
    expect(auditChangeDescription({ key: 'roles', added: ['1', '2'], removed: ['3'] })).toBe(
      'Added 1, 2 • Removed 3'
    );
    expect(auditChangeDescription({ key: 'roles', added: [{ id: '1', name: 'Moderators' }] })).toBe(
      'Added Moderators'
    );
    expect(auditChangeDescription({ key: 'enabled', old_value: false, new_value: true })).toBe(
      'No → Yes'
    );
    expect(auditFieldLabel('rtc_region')).toBe('RTC region');
  });

  it('formats recent timestamps deterministically', () => {
    const now = new Date('2026-08-26T12:00:00Z');
    expect(auditRelativeTime('2026-08-26T11:58:00Z', now)).toBe('2 minutes ago');
    expect(auditRelativeTime('2026-08-25T12:00:00Z', now)).toBe('1 day ago');
  });
});
