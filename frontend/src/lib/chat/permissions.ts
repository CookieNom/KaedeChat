import { Permission } from '$lib/generated/permissions';
import { entityKey } from './refs';
import type { Channel } from './types';

export function hasAllPermissions(effective: bigint, required: bigint): boolean {
  return (
    (effective & Permission.ADMINISTRATOR) === Permission.ADMINISTRATOR ||
    (effective & required) === required
  );
}

export function hasAnyPermission(effective: bigint, requested: bigint): boolean {
  return (
    (effective & Permission.ADMINISTRATOR) === Permission.ADMINISTRATOR ||
    (effective & requested) !== 0n
  );
}

export function canReadChannelHistory(effective: bigint): boolean {
  return hasAllPermissions(effective, Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY);
}

export function reconcileChannelPermissionProjection(
  current: Channel[] | undefined,
  projection: Channel[],
  retainMissing: (channel: Channel) => boolean = () => false
): Channel[] {
  const existing = current ?? [];
  const existingByKey = new Map(existing.map((channel) => [entityKey(channel), channel]));
  const projectedKeys = new Set(projection.map(entityKey));
  const reconciled = [
    ...projection.map((projected) => {
      const channel = existingByKey.get(entityKey(projected));
      if (!channel || channel.permissions === projected.permissions) return channel ?? projected;
      return { ...channel, permissions: projected.permissions };
    }),
    ...existing.filter(
      (channel) => retainMissing(channel) && !projectedKeys.has(entityKey(channel))
    )
  ];
  return reconciled.length === existing.length &&
    reconciled.every((channel, index) => channel === existing[index])
    ? existing
    : reconciled;
}
