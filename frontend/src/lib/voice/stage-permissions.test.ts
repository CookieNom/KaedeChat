import { describe, expect, it } from 'vitest';
import { Permission } from '$lib/generated/permissions';
import {
  STAGE_MODERATOR_PERMISSIONS,
  canCreateScheduledEventInChannel,
  canManageScheduledEventInChannel,
  canManageStageChannel,
  canServerDeafenInChannel
} from './stage-permissions';

const channel = (type: number, permissions: bigint) => ({
  type,
  permissions: permissions.toString()
});

describe('Stage permission predicates', () => {
  it('requires the complete moderator trio for Stage lifecycle controls', () => {
    expect(canManageStageChannel(channel(13, STAGE_MODERATOR_PERMISSIONS))).toBe(true);
    expect(
      canManageStageChannel(channel(13, Permission.MANAGE_CHANNELS | Permission.MUTE_MEMBERS))
    ).toBe(false);
    expect(canManageStageChannel(channel(13, Permission.ADMINISTRATOR))).toBe(true);
    expect(canManageStageChannel(channel(2, STAGE_MODERATOR_PERMISSIONS))).toBe(false);
  });

  it('uses Stage and voice-specific scheduled-event access', () => {
    expect(
      canCreateScheduledEventInChannel(
        channel(13, Permission.CREATE_EVENTS | STAGE_MODERATOR_PERMISSIONS)
      )
    ).toBe(true);
    expect(canCreateScheduledEventInChannel(channel(13, Permission.CREATE_EVENTS))).toBe(false);
    expect(
      canCreateScheduledEventInChannel(
        channel(2, Permission.CREATE_EVENTS | Permission.VIEW_CHANNEL | Permission.CONNECT)
      )
    ).toBe(true);
    expect(
      canCreateScheduledEventInChannel(channel(2, Permission.CREATE_EVENTS | Permission.CONNECT))
    ).toBe(false);
  });

  it('preserves own-vs-other management while requiring Stage moderation', () => {
    expect(
      canManageScheduledEventInChannel(
        channel(13, Permission.CREATE_EVENTS | STAGE_MODERATOR_PERMISSIONS),
        true
      )
    ).toBe(true);
    expect(
      canManageScheduledEventInChannel(
        channel(13, Permission.MANAGE_EVENTS | STAGE_MODERATOR_PERMISSIONS),
        false
      )
    ).toBe(true);
    expect(canManageScheduledEventInChannel(channel(13, Permission.MANAGE_EVENTS), false)).toBe(
      false
    );
    expect(
      canManageScheduledEventInChannel(
        channel(2, Permission.CREATE_EVENTS | Permission.VIEW_CHANNEL | Permission.CONNECT),
        true
      )
    ).toBe(true);
  });

  it('never exposes server-deafen in Stage channels', () => {
    expect(canServerDeafenInChannel(channel(13, Permission.DEAFEN_MEMBERS))).toBe(false);
    expect(canServerDeafenInChannel(channel(2, Permission.DEAFEN_MEMBERS))).toBe(true);
  });
});
