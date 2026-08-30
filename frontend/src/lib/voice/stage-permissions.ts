import { Permission } from '$lib/generated/permissions';
import type { Channel } from '$lib/chat/types';

export const STAGE_MODERATOR_PERMISSIONS =
  Permission.MANAGE_CHANNELS | Permission.MUTE_MEMBERS | Permission.MOVE_MEMBERS;

function permissionBits(channel: Pick<Channel, 'permissions'>): bigint {
  try {
    return BigInt(channel.permissions ?? '0');
  } catch {
    return 0n;
  }
}

function allowsAll(channel: Pick<Channel, 'permissions'>, required: bigint): boolean {
  const effective = permissionBits(channel);
  return Boolean(effective & Permission.ADMINISTRATOR) || (effective & required) === required;
}

function allowsAny(channel: Pick<Channel, 'permissions'>, required: bigint): boolean {
  const effective = permissionBits(channel);
  return Boolean(effective & Permission.ADMINISTRATOR) || Boolean(effective & required);
}

export function canManageStageChannel(channel: Pick<Channel, 'permissions' | 'type'>): boolean {
  return channel.type === 13 && allowsAll(channel, STAGE_MODERATOR_PERMISSIONS);
}

export function canCreateScheduledEventInChannel(
  channel: Pick<Channel, 'permissions' | 'type'>
): boolean {
  if (channel.type === 13) {
    return allowsAll(channel, Permission.CREATE_EVENTS | STAGE_MODERATOR_PERMISSIONS);
  }
  return (
    channel.type === 2 &&
    allowsAll(channel, Permission.CREATE_EVENTS | Permission.VIEW_CHANNEL | Permission.CONNECT)
  );
}

export function canManageScheduledEventInChannel(
  channel: Pick<Channel, 'permissions' | 'type'>,
  ownEvent: boolean
): boolean {
  if (channel.type !== 2 && channel.type !== 13) return false;
  const channelAccess =
    channel.type === 13
      ? allowsAll(channel, STAGE_MODERATOR_PERMISSIONS)
      : allowsAll(channel, Permission.VIEW_CHANNEL | Permission.CONNECT);
  if (!channelAccess) return false;
  return ownEvent
    ? allowsAny(channel, Permission.CREATE_EVENTS | Permission.MANAGE_EVENTS)
    : allowsAll(channel, Permission.MANAGE_EVENTS);
}

export function canServerDeafenInChannel(channel: Pick<Channel, 'permissions' | 'type'>): boolean {
  return channel.type !== 13 && allowsAll(channel, Permission.DEAFEN_MEMBERS);
}
