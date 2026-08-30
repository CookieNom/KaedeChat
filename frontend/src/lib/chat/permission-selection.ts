import { PERMISSION_METADATA, type PermissionMetadata } from '$lib/generated/permissions';

export function permissionMask(value: string): bigint {
  const normalized = String(value).trim();
  if (!/^\d+$/.test(normalized)) {
    throw new Error('Permissions must be a non-negative whole number.');
  }
  return BigInt(normalized);
}

export function permissionSelected(value: string, bit: bigint): boolean {
  return Boolean(permissionMask(value) & bit);
}

export function setPermissionSelected(value: string, bit: bigint, selected: boolean): string {
  const current = permissionMask(value);
  return (selected ? current | bit : current & ~bit).toString();
}

export function selectedPermissionMetadata(value: string): PermissionMetadata[] {
  const current = permissionMask(value);
  return PERMISSION_METADATA.filter((item) => Boolean(current & item.bit));
}
