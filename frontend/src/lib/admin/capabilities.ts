export function hasAdminCapability(
  capabilities: readonly string[] | null | undefined,
  capability: string
): boolean {
  return capabilities?.includes('*') === true || capabilities?.includes(capability) === true;
}
