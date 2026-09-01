import { Permission } from '$lib/generated/permissions';

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
